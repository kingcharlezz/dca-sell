#!/usr/bin/env python3
import asyncio
import yaml
from functools import partial
from typing import Optional

import typer

from bittensor_wallet import Wallet
from bittensor_wallet.errors import KeyFileError
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from async_substrate_interface.errors import SubstrateRequestException
from bittensor_cli.src.bittensor.balances import Balance
from bittensor_cli.src.bittensor.utils import (
    console,
    err_console,
    print_verbose,
    print_error,
)

# For type checking only; adjust the import path as needed.
from bittensor_cli.src.bittensor.subtensor_interface import SubtensorInterface


# ---------------------------------------------------------------------------
# --- Extrinsics (reused from your provided unstaking code) ---------------
# ---------------------------------------------------------------------------
async def _unstake_extrinsic(
    wallet: Wallet,
    subtensor: SubtensorInterface,
    netuid: int,
    amount: Balance,
    current_stake: Balance,
    hotkey_ss58: str,
    status: Optional[Console] = None,
) -> None:
    """Execute a standard unstake extrinsic."""
    err_out = partial(print_error, status=status)
    failure_prelude = (
        f":cross_mark: [red]Failed[/red] to unstake {amount} on Netuid {netuid}"
    )

    if status:
        status.update(
            f"\n:satellite: Unstaking {amount} from {hotkey_ss58} on netuid: {netuid} ..."
        )

    current_balance = await subtensor.get_balance(wallet.coldkeypub.ss58_address)
    call = await subtensor.substrate.compose_call(
        call_module="SubtensorModule",
        call_function="remove_stake",
        call_params={
            "hotkey": hotkey_ss58,
            "netuid": netuid,
            "amount_unstaked": amount.rao,
        },
    )
    extrinsic = await subtensor.substrate.create_signed_extrinsic(
        call=call, keypair=wallet.coldkey
    )

    try:
        response = await subtensor.substrate.submit_extrinsic(
            extrinsic, wait_for_inclusion=True, wait_for_finalization=False
        )
        await response.process_events()

        if not await response.is_success:
            err_out(
                f"{failure_prelude} with error: "
                f"{format_error_message(await response.error_message, subtensor.substrate)}"
            )
            return

        # Fetch latest balance and stake
        block_hash = await subtensor.substrate.get_chain_head()
        new_balance, new_stake = await asyncio.gather(
            subtensor.get_balance(wallet.coldkeypub.ss58_address, block_hash),
            subtensor.get_stake(
                hotkey_ss58=hotkey_ss58,
                coldkey_ss58=wallet.coldkeypub.ss58_address,
                netuid=netuid,
                block_hash=block_hash,
            ),
        )

        console.print(":white_heavy_check_mark: [green]Finalized[/green]")
        console.print(
            f"Balance:\n  [blue]{current_balance}[/blue] :arrow_right: [{new_balance}]"
        )
        console.print(
            f"Subnet: [{netuid}] Stake:\n  [blue]{current_stake}[/blue] :arrow_right: [{new_stake}]"
        )

    except Exception as e:
        err_out(f"{failure_prelude} with error: {str(e)}")


async def _safe_unstake_extrinsic(
    wallet: Wallet,
    subtensor: SubtensorInterface,
    netuid: int,
    amount: Balance,
    current_stake: Balance,
    hotkey_ss58: str,
    price_limit: Balance,
    allow_partial_stake: bool,
    status: Optional[Console] = None,
) -> None:
    """Execute a safe unstake extrinsic with price limit and partial unstake logic."""
    err_out = partial(print_error, status=status)
    failure_prelude = (
        f":cross_mark: [red]Failed[/red] to unstake {amount} on Netuid {netuid}"
    )

    if status:
        status.update(
            f"\n:satellite: Unstaking {amount} from {hotkey_ss58} on netuid: {netuid} ..."
        )

    block_hash = await subtensor.substrate.get_chain_head()

    current_balance, next_nonce, current_stake = await asyncio.gather(
        subtensor.get_balance(wallet.coldkeypub.ss58_address, block_hash),
        subtensor.substrate.get_account_next_index(wallet.coldkeypub.ss58_address),
        subtensor.get_stake(
            hotkey_ss58=hotkey_ss58,
            coldkey_ss58=wallet.coldkeypub.ss58_address,
            netuid=netuid,
        ),
    )

    call = await subtensor.substrate.compose_call(
        call_module="SubtensorModule",
        call_function="remove_stake_limit",
        call_params={
            "hotkey": hotkey_ss58,
            "netuid": netuid,
            "amount_unstaked": amount.rao,
            "limit_price": price_limit,
            "allow_partial": allow_partial_stake,
        },
    )

    extrinsic = await subtensor.substrate.create_signed_extrinsic(
        call=call, keypair=wallet.coldkey, nonce=next_nonce
    )

    try:
        response = await subtensor.substrate.submit_extrinsic(
            extrinsic, wait_for_inclusion=True, wait_for_finalization=False
        )
    except SubstrateRequestException as e:
        if "Custom error: 8" in str(e):
            print_error(
                f"\n{failure_prelude}: Price exceeded tolerance limit. "
                f"Transaction rejected because partial unstaking is disabled. "
                f"Either increase price tolerance or enable partial unstaking.",
                status=status,
            )
            return
        else:
            err_out(f"\n{failure_prelude} with error: {str(e)}")
        return

    await response.process_events()
    if not await response.is_success:
        err_out(f"\n{failure_prelude} with error: {str(await response.error_message)}")
        return

    block_hash = await subtensor.substrate.get_chain_head()
    new_balance, new_stake = await asyncio.gather(
        subtensor.get_balance(wallet.coldkeypub.ss58_address, block_hash),
        subtensor.get_stake(
            hotkey_ss58=hotkey_ss58,
            coldkey_ss58=wallet.coldkeypub.ss58_address,
            netuid=netuid,
            block_hash=block_hash,
        ),
    )

    console.print(":white_heavy_check_mark: [green]Finalized[/green]")
    console.print(
        f"Balance:\n  [blue]{current_balance}[/blue] :arrow_right: [{new_balance}]"
    )

    amount_unstaked = current_stake - new_stake
    if allow_partial_stake and (amount_unstaked != amount):
        console.print(
            "Partial unstake transaction. Unstaked:\n"
            f"  {amount_unstaked} instead of {amount}"
        )

    console.print(
        f"Subnet: [{netuid}] Stake:\n  [blue]{current_stake}[/blue] :arrow_right: [{new_stake}]"
    )


# ---------------------------------------------------------------------------
# --- Helper: Slippage Calculation ------------------------------------------
# ---------------------------------------------------------------------------
def _calculate_slippage(subnet_info, amount: Balance) -> tuple[Balance, str, float]:
    """
    Calculate the received amount and slippage percentage for unstaking.

    Returns:
      - received_amount: Balance after slippage.
      - slippage_pct: Formatted string (e.g., "4.5000 %").
      - slippage_pct_float: Slippage as a float percentage.
    """
    received_amount, _, slippage_pct_float = subnet_info.alpha_to_tao_with_slippage(
        amount
    )
    if subnet_info.is_dynamic:
        slippage_pct = f"{slippage_pct_float:.4f} %"
    else:
        slippage_pct_float = 0
        slippage_pct = "[red]N/A[/red]"
    return received_amount, slippage_pct, slippage_pct_float


# ---------------------------------------------------------------------------
# --- DCA Unstake (Sell) Loop ------------------------------------------------
# ---------------------------------------------------------------------------
async def dca_unstake_loop(wallet: Wallet, subtensor: SubtensorInterface, config: dict):
    """
    Continuously sell (unstake) a portion of your bag from allowed subnets at specified intervals.
    
    The script uses two percentage values to control the selling:
      - total_sell_percentage: The maximum portion of the base stake that will be sold
      - unstake_percentage: The portion of base stake to sell in each cycle
    
    For each subnet:
      - The current stake is locked as the base stake upon the first sale
      - The total amount to sell is calculated as (base stake * total_sell_percentage / 100)
      - In each cycle, it sells (unstake_percentage)% of the base stake
      - The script ensures that the cumulative sold amount never exceeds the total_sell_percentage
    """
    dca_config = config.get("dca", {})
    normal_interval = dca_config.get("normal_interval", 60)
    low_balance_interval = dca_config.get("low_balance_interval", 300)
    unstake_percentage = dca_config.get("unstake_percentage", 1)
    total_sell_percentage = dca_config.get("total_sell_percentage", 10)
    allowed_subnets = dca_config.get("allowed_subnets", [])
    slippage_tolerance = dca_config.get("slippage_tolerance", 5)
    min_stake_threshold = dca_config.get("min_stake_threshold", 0.0001)

    if not allowed_subnets:
        console.print("[red]No allowed subnets specified in config.[/red]")
        return

    console.print(
        f"[bold green]Starting DCA sell loop:[/bold green] "
        f"selling up to {total_sell_percentage}% of stake from subnets: {allowed_subnets} "
        f"in {unstake_percentage}% increments, "
        f"with a maximum slippage tolerance of {slippage_tolerance}% "
        f"and minimum stake threshold of {min_stake_threshold} tao."
    )

    # Fetch dynamic subnet info once before looping.
    all_sn_dynamic_info_list = await subtensor.all_subnets()
    all_sn_dynamic_info = {info.netuid: info for info in all_sn_dynamic_info_list}

    # Dictionary to store the base stake (initial stake used for sale calculations)
    base_stakes: dict[int, Balance] = {}
    
    # Dictionary to store the last observed stake for each subnet
    last_stakes: dict[int, Balance] = {}
    
    # Dictionary to track if we're waiting for stake increase on each subnet
    waiting_for_increase: dict[int, bool] = {netuid: False for netuid in allowed_subnets}

    # Track cumulative sold amounts
    cumulative_sold: dict[int, float] = {}

    while True:
        try:
            stake_found = False
            for netuid in allowed_subnets:
                subnet_info = all_sn_dynamic_info.get(netuid)
                if subnet_info is None:
                    console.print(f"[red]No dynamic info found for subnet {netuid}. Skipping.[/red]")
                    continue

                # Fetch current stake for the given subnet with error handling.
                try:
                    stake = await subtensor.get_stake(
                        hotkey_ss58=wallet.hotkey.ss58_address,
                        coldkey_ss58=wallet.coldkeypub.ss58_address,
                        netuid=netuid,
                    )
                except Exception as fetch_err:
                    console.print(f"[red]Failed to fetch stake for subnet {netuid}: {fetch_err}. Skipping this subnet.[/red]")
                    continue

                # Update last stake and check if we should resume selling.
                if netuid in last_stakes:
                    if waiting_for_increase[netuid] and stake.tao <= last_stakes[netuid].tao:
                        console.print(f"[yellow]Waiting for stake increase on subnet {netuid} before resuming selling.[/yellow]")
                        continue
                    elif waiting_for_increase[netuid] and stake.tao > last_stakes[netuid].tao:
                        console.print(f"[green]Stake increased on subnet {netuid}. Resuming selling.[/green]")
                        waiting_for_increase[netuid] = False
                last_stakes[netuid] = stake

                if stake.tao > min_stake_threshold:
                    stake_found = True

                    # Lock the base stake only once per subnet
                    if netuid not in base_stakes:
                        base_stakes[netuid] = stake
                        cumulative_sold[netuid] = 0.0
                        console.print(f"[green]Locked base stake for subnet {netuid} at {stake}.[/green]")

                    total_to_sell = base_stakes[netuid].tao * (total_sell_percentage / 100)
                    if cumulative_sold[netuid] >= total_to_sell:
                        console.print(
                            f"[green]Sell target reached for subnet {netuid}. "
                            f"Total sold: {cumulative_sold[netuid]:.4f}/{total_to_sell:.4f} tao.[/green]"
                        )
                        continue

                    # Calculate sale amount as a percentage of the total_sell_percentage
                    # If total_sell_percentage is 10% and unstake_percentage is 1%,
                    # then we sell 0.1% of base stake per cycle
                    cycle_sell_percentage = (total_sell_percentage * unstake_percentage) / 100
                    planned_sale_amount = base_stakes[netuid].tao * (cycle_sell_percentage / 100)
                    available_to_sell = total_to_sell - cumulative_sold[netuid]
                    sale_amount_tao = min(planned_sale_amount, available_to_sell, stake.tao)

                    if sale_amount_tao <= min_stake_threshold:
                        console.print(f"[yellow]Sale amount too low on subnet {netuid}. Waiting for increase.[/yellow]")
                        waiting_for_increase[netuid] = True
                        continue

                    amount_to_unstake = Balance.from_tao(sale_amount_tao)
                    amount_to_unstake.set_unit(netuid)

                    # Calculate slippage for this sale amount.
                    received_amount, slippage_str, slippage_pct_float = _calculate_slippage(
                        subnet_info, amount_to_unstake
                    )
                    if slippage_pct_float > slippage_tolerance:
                        console.print(
                            f"[red]Slippage {slippage_str} exceeds tolerance of {slippage_tolerance}% for subnet {netuid}. Skipping sale.[/red]"
                        )
                        continue

                    # Calculate a price limit based on the tolerance (only for dynamic subnets).
                    if subnet_info.is_dynamic:
                        tolerance_fraction = slippage_tolerance / 100
                        price_limit = subnet_info.price.rao * (1 - tolerance_fraction)
                    else:
                        price_limit = Balance.from_tao(0)

                    console.print(
                        f"[blue]Initiating sale:[/blue] Selling {amount_to_unstake} "
                        f"({cycle_sell_percentage:.3f}% of base {base_stakes[netuid]}) from subnet {netuid} "
                        f"with expected slippage {slippage_str}. "
                        f"Progress: {cumulative_sold[netuid]:.4f}/{total_to_sell:.4f} tao "
                        f"({(cumulative_sold[netuid]/total_to_sell*100):.1f}% of target)"
                    )

                    try:
                        await _safe_unstake_extrinsic(
                            wallet=wallet,
                            subtensor=subtensor,
                            netuid=netuid,
                            amount=amount_to_unstake,
                            current_stake=stake,
                            hotkey_ss58=wallet.hotkey.ss58_address,
                            price_limit=price_limit,
                            allow_partial_stake=True,
                            status=None,
                        )
                        # Update the cumulative sold amount
                        cumulative_sold[netuid] += sale_amount_tao
                    except Exception as unstake_err:
                        console.print(f"[red]Error selling on subnet {netuid}: {unstake_err}[/red]")
                        continue

                else:
                    console.print(f"[yellow]Stake below minimum threshold on subnet {netuid}. Waiting for increase.[/yellow]")
                    waiting_for_increase[netuid] = True

            if stake_found:
                console.print(
                    f"[green]Sell operations executed. Waiting {normal_interval} seconds for next cycle.[/green]"
                )
                await asyncio.sleep(normal_interval)
            else:
                console.print(
                    f"[yellow]No stakes above minimum threshold. Checking again in {low_balance_interval} seconds.[/yellow]"
                )
                await asyncio.sleep(low_balance_interval)
        except Exception as loop_err:
            console.print(f"[red]Encountered unhandled error in sell loop: {loop_err}. Sleeping for {low_balance_interval} seconds to avoid spamming the chain.[/red]")
            await asyncio.sleep(low_balance_interval)



# ---------------------------------------------------------------------------
# --- Main Function ---------------------------------------------------------
# ---------------------------------------------------------------------------
async def main():
    """
    Main entry point:
      - Loads configuration from config.yaml.
      - Creates the wallet.
      - Connects to the subtensor network using an async context.
      - Runs the DCA sell loop.
    
    Your config.yaml must include the following structure:
    
    wallet:
      path: <bittensor default wallet path, e.g., "~/.bittensor/wallets/default">
      name: <wallet name>
      hotkey: <hotkey string or address>
    
    subtensor:
      network: <network name>
      host: <node websocket URL>
      port: <node port>
    
    dca:
      total_sell_percentage: 10    # Total portion of stake to sell (e.g., 10 means sell up to 10% of total stake)
      unstake_percentage: 1        # Percentage to unstake per cycle (e.g., 1 means sell 1% of base stake per cycle)
      min_stake_threshold: 0.1     # Minimum stake threshold to continue unstaking (in tao)
      normal_interval: 60          # Run unstake every 60 seconds when stake exists
      low_balance_interval: 300    # Check every 300 seconds when no stake is found
      allowed_subnets:
        - 64                      # List of allowed netuids to unstake from
      slippage_tolerance: 5        # Maximum allowed slippage percentage
    """
    try:
        with open("config.yaml", "r") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        console.print(f"[red]Error loading config.yaml: {e}[/red]")
        raise typer.Exit()

    # Create the wallet instance using settings from the config file.
    try:
        wallet_config = config.get("wallet", {})
        wallet = Wallet(
            path=wallet_config.get("path", "~/.bittensor/wallets/default"),
            name=wallet_config.get("name", "default"),
            hotkey=wallet_config.get("hotkey", ""),
        )
    except Exception as e:
        console.print(f"[red]Error creating wallet: {e}[/red]")
        raise typer.Exit()

    # Unlock the coldkey – adjust as necessary (e.g. prompt for password if required)
    try:
        wallet.unlock_coldkey()
    except KeyFileError:
        err_console.print("Error decrypting coldkey (possibly incorrect password)")
        raise typer.Exit()

    # Connect to the subtensor network using the provided config settings.
    try:
        async with SubtensorInterface(**config.get("subtensor", {})) as subtensor:
            await dca_unstake_loop(wallet, subtensor, config)
    except Exception as e:
        console.print(f"[red]Error connecting to subtensor or running loop: {e}[/red]")
        raise typer.Exit()


if __name__ == "__main__":
    asyncio.run(main())
