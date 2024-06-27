import asyncio
import json
from starknet_py.contract import Contract
from starknet_py.net.full_node_client import FullNodeClient
from datetime import datetime
import pandas as pd
import os
from itertools import permutations

# Configs
# In addition to the configs below, make sure to add example.env vars
NODE_URL = "https://starknet-mainnet.public.blastapi.io"
ELIGIBLE = ["STRK", "ETH", "USDC", "USDT"]
STABLES = ["USDC", "USDT"]
SCALE = 10**18
MARKETS = [
    {
        "asset": "0x049d36570d4e46f48e99674bd3fcc84644ddd6b96f7c741b1562b82f9e004dc7",
        "name": "Ether",
        "symbol": "ETH",
        "decimals": 18,
        "vToken": "0x04c73766862dc67b3c3a06f2fae8558fb1253d7a05e1d784b5bc5fc566545ec3"
    }, 
    {
        "asset": "0x03fe2b97c1fd336e750087d68b9b867997fd64a2661ff3ca5a7c771641e8e7ac",
        "name": "Wrapped BTC",
        "symbol": "WBTC",
        "decimals": 8,
        "vToken": "0x065d685590870b0090851e7a815234eb2ab939187f88da99191852481cbcdbdb"
    },
    {
        "asset": "0x053c91253bc9682c04929ca02ed00b3e423f6710d2ee7e0d5ebb06f3ecf368a8",
        "name": "USD Coin",
        "symbol": "USDC",
        "decimals": 6,
        "vToken": "0x026071850d0637590bfcb8ce4ca69e9db2152727cabc598f3861fecfa8676dbf"
    },
    {
        "asset": "0x068f5c6a61780768455de69077e07e89787839bf8166decfbf92b645209c0fb8",
        "name": "Tether USD",
        "symbol": "USDT",
        "decimals": 6,
        "vToken": "0x043daf194cb18291bd725e83634fd3c6144a0ab3c1f1b2e62a5a2e5c8492f7ee"
    },
    {
        "asset": "0x042b8f0484674ca266ac5d08e4ac6a3fe65bd3129795def2dca5c34ecc5f96d2",
        "name": "Starknet Wrapped Staked Ether",
        "symbol": "wstETH",
        "decimals": 18,
        "vToken": "0x9041f1270939f0e472a4d3abcb289042b4421f1a74e6f51499af62a0bc2b5e"
    },
    {
        "asset": "0x04718f5a0fc34cc1af16a1cdee98ffb20c31f5cd61d6ab07201858f4287c938d",
        "name": "Starknet Token",
        "symbol": "STRK",
        "decimals": 18,
        "vToken": "0x042d3960696056bfaf9d65a4f7a57b7417e7fb0e0d44d4475dd845806381e089"
    }
]

# Fetch data for a specific market from Vesu singleton directly
async def get_market_info(market_info, singleton_contract, provider, POOL):
    block = await provider.get_block_number()
    now = datetime.now()
    formatted_date = now.strftime("%Y-%m-%d")
    # singleton.asset_config_unsafe returns per-current block market data
    # - reserve: the remaining liquidity in the market
    # - total_nominal_debt: the total outstanding nominal (excluding accrued interest) debt
    # - last_rate_accumulator: the index tracking accrued interest per current block
    #
    # Notes:
    # - the pool (identified by POOL) does not support "recursive" supply/borrowing
    #   so supply = non-recursive-supply by default
    # - rate_accumulator is an interest accrual index that converts to users' total
    #   borrowed by multiplication with total_nominal_debt
    # - we add the raw rate_accumulator to the return values so 'get_stables_info' 
    #   can reuse, then drop the column of the DataFrame at the end again
    asset_config = (await singleton_contract.functions["asset_config_unsafe"].call(
        POOL, int(market_info['asset'], base=16)))[0][0]
    asset_scale = asset_config['scale']
    reserve = asset_config['reserve'] / asset_scale
    rate_accumulator = asset_config['last_rate_accumulator'] / SCALE
    total_borrowed = rate_accumulator * asset_config['total_nominal_debt']  / SCALE
    total_supplied = reserve + total_borrowed
    # lending_index_rate is fetched from the pool's vTokens directly
    lending_index_rate = (await asyncio.gather(get_index(market_info, provider)))[0]
    return {
        "protocol": "Vesu",
        "date": formatted_date,
        "market": market_info['asset'],
        "tokenSymbol": market_info['symbol'],
        "supply_token": total_supplied,
        "borrow_token": total_borrowed,
        "net_supply_token": reserve,
        "non_recursive_supply_token": total_supplied,
        "block_height": block,
        "lending_index_rate": lending_index_rate / SCALE,
        "rate_accumulator": rate_accumulator
    }

# Fetch data for stablecoins combined
# The extension contract tracks total debt and supplied liquidity per "lending pair".
# We thus clean total supply from recursive supply/borrowing by deducting the debt from
# the recursive lending pairs (e.g. USDC/USDT, USDT/USDC, etc.)
async def get_stables_info(markets, results_markets, provider, EXTENSION, POOL):
    df_markets = pd.DataFrame(results_markets)
    extension_contract = await Contract.from_address(provider=provider, address=EXTENSION)
    coroutines = [get_pair_info(pair_info, extension_contract, POOL)
                      for pair_info in permutations(markets, 2) 
                      if pair_info[0]['symbol'] in STABLES
                      and pair_info[1]['symbol'] in STABLES]
    results_pairs = await asyncio.gather(*coroutines)
    df_pairs = pd.DataFrame(results_pairs).groupby('asset').sum()
    coroutines = [get_price(market_info, extension_contract, POOL) 
                  for market_info in markets 
                  if market_info['symbol'] in STABLES]
    results_prices = await asyncio.gather(*coroutines)
    df_prices = pd.DataFrame(results_prices)
    total_non_recursive_supplied = 0
    for asset in df_prices.asset:
        supply = df_markets.query('market == @asset').supply_token.iloc[0]
        recursive_borrow = (df_markets.query('market == @asset').rate_accumulator.iloc[0] * 
            df_pairs.query('asset == @asset').recursive_nominal_debt.iloc[0])
        non_recursive_supplied = supply - recursive_borrow
        price = df_prices.query('asset == @asset').price.iloc[0]
        total_non_recursive_supplied += price * max(0, non_recursive_supplied)
    return {
        "protocol": "Vesu",
        "date": df_markets.date[0],
        "market": "0x0stable",
        "tokenSymbol": "STB",
        "supply_token": 0,
        "borrow_token": 0,
        "net_supply_token": 0,
        "non_recursive_supply_token": total_non_recursive_supplied,
        "block_height": df_markets.block_height[0],
        "lending_index_rate": 1.0
    }

# Fetch total supply and debt for a specific lending pair
async def get_pair_info(pair_info, extension_contract, POOL):
    collateral_asset = pair_info[0]['asset']
    debt_asset = pair_info[1]['asset']
    pair_info = (await extension_contract.functions['pairs'].call(
        POOL, int(collateral_asset, base=16), int(debt_asset, base=16)))[0]
    return {
        "asset": debt_asset,
        "recursive_nominal_debt": pair_info['total_nominal_debt'] / SCALE
    }

# Fetch the oracle price for a specific asset (pulls from Pragma)
async def get_price(market_info, extension_contract, POOL):
    asset = market_info['asset']
    asset_price = (await extension_contract.functions['price'].call(
        POOL, int(asset, base=16)))[0]['value']
    return {
        "asset": asset,
        "price": asset_price / SCALE
    }

# Fetch the lending index rate (pulls from vToken)
async def get_index(market_info, provider):
    vToken_contract = await Contract.from_address(provider=provider, address=market_info['vToken'])
    index = (await vToken_contract.functions['convert_to_assets'].call(
        int(SCALE)))[0]
    return index

async def main():
    """
    Supply your calculation here according to the Guidelines.
    """
    SINGLETON = os.getenv('SINGLETON')
    EXTENSION = os.getenv('EXTENSION')
    POOL = int(os.getenv('POOL'))
    provider = FullNodeClient(node_url=NODE_URL)
    singleton_contract = await Contract.from_address(provider=provider, address=SINGLETON)
    coroutines = [get_market_info(market_info, singleton_contract, provider, POOL)
                    for market_info in MARKETS 
                    if market_info['symbol'] in ELIGIBLE]
    results_markets = await asyncio.gather(*coroutines)
    results_stables = await get_stables_info(MARKETS, results_markets, provider, EXTENSION, POOL)
    results_markets.append(results_stables)
    df = pd.DataFrame(results_markets)
    df.drop('rate_accumulator', axis=1, inplace=True)
    print(df.to_string())
    return df

if __name__ == "__main__":
    asyncio.run(main())
