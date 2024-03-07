import asyncio, json
from pandas import DataFrame
from starknet_py.contract import Contract
from starknet_py.net.full_node_client import FullNodeClient
from datetime import datetime
import pandas as pd
from collections import OrderedDict

rTokenAbi = json.load(open('./hashstack/rToken.abi.json'))
dTokenAbi = json.load(open('./hashstack/dToken.abi.json'))

# Returns row for each token
# Contains supply and lending index
async def get_token_info(tokenInfo, provider):
    supply_contract = Contract(
        address=tokenInfo['rToken'], abi=rTokenAbi, provider=provider
    )

    (total_assets,) = await supply_contract.functions["total_assets"].call()

    (_, lending_rate) = await supply_contract.functions["exchange_rate"].call()

    borrow_contract = Contract(
        address=tokenInfo['dToken'], abi=dTokenAbi, provider=provider
    )

    (total_debt,) = await borrow_contract.functions["totalDebt"].call()

    net_supply = total_assets - total_debt
    block = await provider.get_block_number()
    now = datetime.now()
    formatted_date = now.strftime("%Y-%m-%d")

    return {
        "protocol": "Hashstack",
        "date": formatted_date,
        "market": tokenInfo["address"],
        "tokenSymbol": tokenInfo["name"],
        "supply_token": total_assets,
        "borrow_token": total_debt,
        "net_supply_token": net_supply,
        "non_recursive_supply_token": total_assets,
        "block_height": block,
        "lending_index_rate": lending_rate/10**18
    }

# Define functions
async def main():
    """
    Supply your calculation here according to the Guidelines.
    """

    node_url = "https://starknet-mainnet.public.blastapi.io"
    provider = FullNodeClient(node_url=node_url)

    with open('./hashstack/tokens.json', 'r') as f:
        tokens = json.load(f)
        coroutines = [get_token_info(tokenInfo, provider) for tokenInfo in tokens]
        gathered_results = await asyncio.gather(*coroutines)
        
        df = pd.DataFrame(gathered_results)
        df.to_csv('output_hashstack.csv', index=False)

        return gathered_results

if __name__ == "__main__":
    asyncio.run(main())