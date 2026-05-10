#!/usr/bin/env python3
"""
BTC Ledger Wallet Tracker

Bitcoin wallet tracking system for Ledger hardware wallet-derived addresses.
Scans the Bitcoin blockchain to find all addresses with transaction history,
calculates current balances, and tracks internal wallet-to-wallet transfers.
"""

import os
import signal
import sys
import time
import json
from typing import Any, Dict, List, Optional, Tuple

import requests
from bip_utils import Bip44, Bip44Coins, Bip44Changes, P2WPKHAddrEncoder

#-------------------------------------------------------------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, 'config.py')
DATA_FILE = os.path.join(SCRIPT_DIR, 'ledger.json')

#-------------------------------------------------------------------------------------------------------------------------------

# Type aliases for clarity
AddressData = Dict[str, Any]
TransactionData = Dict[str, Any]

#-------------------------------------------------------------------------------------------------------------------------------

def load_config() -> AddressData:
    """
    Load configuration from config.py.

    Returns:
        Dictionary containing config values (xpub, SCAN_GAP_THRESHOLD, SCAN_BATCH_SIZE)
    """
    try:
        config: AddressData = {}
        exec(open(CONFIG_FILE).read(), config)
        return config
    except Exception:
        return {}

#-------------------------------------------------------------------------------------------------------------------------------

def load_xpub() -> str:
    """
    Load the xpub value from config.py.

    Returns:
        The BIP84 extended public key string
    """
    try:
        config: AddressData = {}
        exec(open(CONFIG_FILE).read(), config)
        return config.get('xpub', '')
    except Exception:
        return ''

#-------------------------------------------------------------------------------------------------------------------------------

def load_checkpoint() -> Optional[AddressData]:
    """
    Load checkpoint data from ledger.json.

    Returns:
        Dictionary containing checkpoint data, or None if file doesn't exist
    """
    try:
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return None

#-------------------------------------------------------------------------------------------------------------------------------

def get_btc_price() -> Optional[float]:
    """
    Fetch current BTC/USD price from Kraken API.

    Returns:
        Current BTC price in USD, or None if request fails
    """
    try:
        r = requests.get('https://api.kraken.com/0/public/Ticker?pair=XBTUSD', timeout=10)
        if r.status_code == 200:
            return float(r.json()['result']['XXBTZUSD']['c'][0])
    except Exception:
        pass
    return None

#-------------------------------------------------------------------------------------------------------------------------------

def get_address_txs(addr: str) -> Optional[List[TransactionData]]:
    """
    Fetch all transactions for a given Bitcoin address from mempool.space API.

    Args:
        addr: Bitcoin address to query

    Returns:
        List of transaction data, or None if request fails
    """
    time.sleep(1.2)  # Rate limiting to avoid API throttling
    try:
        r = requests.get(f'https://mempool.space/api/address/{addr}/txs', timeout=30)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

#-------------------------------------------------------------------------------------------------------------------------------

def save_checkpoint(
    addresses_data: AddressData,
    total_btc: float,
    xpub: str,
    max_receive_checked: int,
    max_change_checked: int
) -> None:
    """
    Save current scan progress to ledger.json.

    Args:
        addresses_data: Dictionary of address data
        total_btc: Total wallet balance in BTC
        xpub: The xpub being scanned
        max_receive_checked: Highest receive index checked
        max_change_checked: Highest change index checked
    """
    output: AddressData = {
        'xpub': xpub,
        'total_balance_btc': total_btc,
        'max_receive_checked': max_receive_checked,
        'max_change_checked': max_change_checked,
        'addresses': addresses_data
    }
    with open(DATA_FILE, 'w') as f:
        json.dump(output, f, indent=2)

#-------------------------------------------------------------------------------------------------------------------------------

# Global variables for signal handler and checkpoint tracking
_addresses_data: Optional[AddressData] = None
_total_btc: float = 0.0
_price: Optional[float] = None
_xpub: str = ''
_max_receive: int = 0
_max_change: int = 0
_stop_requested: bool = False

#-------------------------------------------------------------------------------------------------------------------------------

def signal_handler(signum: int, frame: Any) -> None:
    """
    Handle Ctrl+C interrupt by saving checkpoint before exit.

    Args:
        signum: Signal number
        frame: Current stack frame
    """
    global _stop_requested
    print("\n\nInterrupted! Saving checkpoint...")
    _stop_requested = True
    if _addresses_data is not None:
        save_checkpoint(_addresses_data, _total_btc, _xpub, _max_receive, _max_change)
        print(f"Saved to {DATA_FILE}")
    print("\nStopped.")
    sys.exit(0)

#-------------------------------------------------------------------------------------------------------------------------------

def derive_addresses(xpub: str, config: AddressData) -> Tuple[AddressData, int, int]:
    """
    Derive BIP84 receive and change addresses from xpub.

    Args:
        xpub: BIP84 extended public key
        config: Configuration dictionary

    Returns:
        Tuple of (address_map, gap_threshold, batch_size)
    """
    bip44 = Bip44.FromExtendedKey(xpub, Bip44Coins.BITCOIN)

    gap_threshold = config.get('SCAN_GAP_THRESHOLD', 20)
    batch_size = config.get('SCAN_BATCH_SIZE', 20)

    addr_map: AddressData = {'receive': {}, 'change': {}}

    for change in [Bip44Changes.CHAIN_EXT, Bip44Changes.CHAIN_INT]:
        chain = 'change' if change == Bip44Changes.CHAIN_INT else 'receive'
        chg = bip44.Change(change)
        for i in range(100):
            pub = chg.AddressIndex(i).PublicKey()
            addr = P2WPKHAddrEncoder.EncodeKey(pub.RawCompressed().ToBytes(), hrp="bc")
            addr_map[chain][addr] = i

    return addr_map, gap_threshold, batch_size

#-------------------------------------------------------------------------------------------------------------------------------

def process_address(addr: str, wallet_addrs: set, addr_map: AddressData) -> Tuple[Optional[AddressData], float]:
    """
    Process a single address: fetch transactions and calculate balance.

    Args:
        addr: Bitcoin address to process
        wallet_addrs: Set of all wallet addresses
        addr_map: Map of address to index/is_change

    Returns:
        Tuple of (address_info, balance) or (None, 0) if no transactions
    """
    txs = get_address_txs(addr)
    if not txs:
        return None, 0.0

    received: int = 0
    spent: int = 0

    # Calculate total received and spent
    for tx in txs:
        for v in tx.get('vout', []):
            if v.get('scriptpubkey_address') == addr:
                received += v.get('value', 0)
        for v in tx.get('vin', []):
            p = v.get('prevout', {})
            if p.get('scriptpubkey_address') == addr:
                spent += p.get('value', 0)

    balance = (received - spent) / 1e8

    if received > 0 or spent > 0:
        trans_list: List[TransactionData] = []

        # Process each transaction
        for tx in txs:
            tx_value_in: int = 0
            tx_value_out: int = 0

            # Calculate value received from this tx
            for v in tx.get('vout', []):
                if v.get('scriptpubkey_address') == addr:
                    tx_value_in += v.get('value', 0)

            # Calculate value spent in this tx
            for v in tx.get('vin', []):
                p = v.get('prevout', {})
                if p.get('scriptpubkey_address') == addr:
                    tx_value_out += p.get('value', 0)

            # Collect inputs (all sending addresses)
            relevant_in: List[TransactionData] = []
            for v in tx.get('vin', []):
                src = v.get('prevout', {}).get('scriptpubkey_address', '')
                value = v.get('prevout', {}).get('value', 0) / 1e8
                relevant_in.append({
                    'address': src,
                    'value': value
                })

            # Collect outputs (only wallet receiving addresses)
            relevant_out: List[TransactionData] = []
            for v in tx.get('vout', []):
                dst = v.get('scriptpubkey_address', '')
                if dst in wallet_addrs:
                    value = v.get('value', 0) / 1e8
                    relevant_out.append({
                        'address': dst,
                        'value': value
                    })

            tx_info: TransactionData = {
                'txid': tx.get('txid', ''),
                'received': tx_value_in / 1e8,
                'spent': tx_value_out / 1e8,
            }
            if relevant_in:
                tx_info['inputs'] = relevant_in
            if relevant_out:
                tx_info['outputs'] = relevant_out

            trans_list.append(tx_info)

        return {
            'is_change': addr_map[addr]['is_change'],
            'index': addr_map[addr]['index'],
            'received': received / 1e8,
            'spent': spent / 1e8,
            'balance': balance,
            'tx_count': len(txs),
            'transactions': trans_list
        }, balance

    return None, 0.0

#-------------------------------------------------------------------------------------------------------------------------------

def verify_and_prompt(addresses_data: AddressData, total_btc: float, price: Optional[float]) -> Tuple[Optional[bool], float]:
    """
    Display addresses with balances and prompt for verification.

    Args:
        addresses_data: Dictionary of address data
        total_btc: Total balance in BTC
        price: Current BTC/USD price

    Returns:
        Tuple of (is_correct, total_balance)
    """
    print("\n" + "=" * 100)
    print("RECEIVE ADDRESSES (Balances):")
    print("=" * 100)

    # Show receive addresses with balance
    receive_addrs = [(a, i) for a, i in addresses_data.items() if i.get('is_change') == False and i.get('balance', 0) > 0]
    receive_addrs.sort(key=lambda x: x[1]['balance'], reverse=True)

    if receive_addrs:
        for addr, info in receive_addrs:
            usd = f"  (${info['balance'] * price:,.2f})" if price else ""
            print(f"{addr}")
            print(f"  Balance: {info['balance']:.8f} BTC {usd}")
            print()

    print("=" * 100)
    print("CHANGE ADDRESSES (Balances):")
    print("=" * 100)

    # Show change addresses with balance
    change_addrs = [(a, i) for a, i in addresses_data.items() if i.get('is_change') == True and i.get('balance', 0) > 0]
    change_addrs.sort(key=lambda x: x[1]['balance'], reverse=True)

    if change_addrs:
        for addr, info in change_addrs:
            usd = f"  (${info['balance'] * price:,.2f})" if price else ""
            print(f"{addr}")
            print(f"  Balance: {info['balance']:.8f} BTC {usd}")
            print()

    total_with_balance = sum(i['balance'] for i in addresses_data.values() if i.get('balance', 0) > 0)

    # Prompt for user confirmation
    while True:
        usd_str = f"  (${total_with_balance * price:,.2f})" if price else ""
        print("\n" + "=" * 100)
        print(f"Total found: {total_with_balance:.8f} BTC {usd_str}")
        print(f"Is this correct? (y/n/q): ", end="")

        try:
            resp = input().strip().lower()
        except EOFError:
            resp = 'q'

        if resp == 'y':
            return True, total_with_balance
        elif resp == 'q':
            return False, total_with_balance
        elif resp == 'n':
            return None, total_with_balance
        print("Please enter y, n, or q")

#-------------------------------------------------------------------------------------------------------------------------------

def scan_chain(
    chain_name: str,
    addr_list: List[str],
    addresses_data: AddressData,
    wallet_addrs: set,
    addr_map: AddressData,
    gap_threshold: int,
    start_idx: int = 0,
    max_to_check: Optional[int] = None
) -> Tuple[int, int]:
    """
    Scan a list of addresses, checking for transaction history.

    Args:
        chain_name: 'receive' or 'change'
        addr_list: List of addresses to check
        addresses_data: Dictionary to store results
        wallet_addrs: Set of all wallet addresses
        addr_map: Map of address to index/is_change
        gap_threshold: Consecutive empty addresses before stopping
        start_idx: Index to start scanning from
        max_to_check: Maximum addresses to check (None for unlimited)

    Returns:
        Tuple of (total_checked, last_index)
    """
    total_checked: int = 0
    empty_count: int = 0

    for idx, addr in enumerate(addr_list):
        if idx < start_idx:
            continue

        if max_to_check and total_checked >= max_to_check:
            break

        # Check if already in checkpoint
        if addr in addresses_data:
            if addresses_data[addr].get('tx_count', 0) > 0:
                empty_count = 0
            else:
                empty_count += 1
            total_checked += 1
            continue

        print(f"[{chain_name} {idx}] Checking {addr[:35]}...", end=" ", flush=True)

        info, balance = process_address(addr, wallet_addrs, {addr: addr_map[addr]})

        if info is not None:
            total_checked += 1
            print(f"{info['tx_count']} txs", end="", flush=True)

            addresses_data[addr] = info

            if balance > 0:
                empty_count = 0
                print(f" -> BALANCE: {balance:.8f} BTC", end="", flush=True)
            else:
                # Has transactions but balance is 0 (already spent) - still counts as active
                empty_count = 0
        else:
            total_checked += 1
            empty_count += 1
            print("no data", end="", flush=True)

        print(flush=True)

        # Stop if gap threshold reached
        if empty_count >= gap_threshold:
            print(f"\nStopping {chain_name} scan after {empty_count} consecutive empty addresses")
            break

        # Progressive save every 5 addresses
        if total_checked > 0 and total_checked % 5 == 0:
            current_btc = sum(i.get('balance', 0) for i in addresses_data.values())
            save_checkpoint(addresses_data, current_btc, _xpub, _max_receive, _max_change)
            print(f"  [Checkpoint saved: {len(addresses_data)} addresses]")

    return total_checked, max(0, idx)

#-------------------------------------------------------------------------------------------------------------------------------

def main() -> None:
    """
    Main entry point for the BTC Ledger Wallet Tracker.
    """
    global _addresses_data, _total_btc, _price, _xpub, _max_receive, _max_change

    signal.signal(signal.SIGINT, signal_handler)

    config = load_config()
    xpub = load_xpub()

    if not xpub:
        print("ERROR: No xpub in config.py")
        return

    gap_threshold = config.get('SCAN_GAP_THRESHOLD', 20)
    batch_size = config.get('SCAN_BATCH_SIZE', 20)

    _xpub = xpub

    print("=" * 100)
    print("BTC Ledger Wallet Tracker")
    print("=" * 100)
    print(f"\nxpub: {xpub[:40]}...")

    price = get_btc_price()
    _price = price
    print(f"BTC Price: ${price:,.2f}" if price else "Could not fetch price")

    # Derive addresses from xpub
    addr_map_data, gap_threshold, batch_size = derive_addresses(xpub, config)
    receive_addrs = sorted(addr_map_data['receive'].keys(), key=lambda a: addr_map_data['receive'][a])
    change_addrs = sorted(addr_map_data['change'].keys(), key=lambda a: addr_map_data['change'][a])

    # Build address map with metadata
    addr_map: AddressData = {}
    for addr in receive_addrs:
        addr_map[addr] = {'is_change': False, 'index': addr_map_data['receive'][addr]}
    for addr in change_addrs:
        addr_map[addr] = {'is_change': True, 'index': addr_map_data['change'][addr]}

    wallet_addrs = set(addr_map.keys())

    # Load checkpoint or start fresh
    checkpoint = load_checkpoint()
    is_first_run = True
    if checkpoint and checkpoint.get('xpub') == xpub:
        print(f"\nResuming from checkpoint...")
        addresses_data = checkpoint.get('addresses', {})
        _max_receive = checkpoint.get('max_receive_checked', 0)
        _max_change = checkpoint.get('max_change_checked', 0)
        if _max_receive > 0 or _max_change > 0:
            is_first_run = False
        print(f"  Already checked: {_max_receive} receive, {_max_change} change addresses")
    else:
        print(f"\nStarting fresh scan...")
        addresses_data = {}
        _max_receive = 0
        _max_change = 0

    # First run scans all addresses; subsequent runs use batch_size
    max_to_receive = len(receive_addrs) if is_first_run else batch_size

    _addresses_data = addresses_data
    _total_btc = 0.0
    _max_receive = _max_receive
    _max_change = _max_change

    print(f"\nDerived {len(receive_addrs)} receive + {len(change_addrs)} change = {len(wallet_addrs)} addresses")
    print(f"Gap threshold: {gap_threshold}, Batch size: {batch_size}")
    print("-" * 100)

    # Scan receive addresses
    print(f"\n=== Scanning receive addresses ===")
    checked, last_idx = scan_chain('receive', receive_addrs, addresses_data, wallet_addrs, addr_map, gap_threshold, _max_receive, max_to_receive)
    _max_receive = max(_max_receive, checked)
    _total_btc = sum(i.get('balance', 0) for i in addresses_data.values())

    total_with_balance = sum(i.get('balance', 0) for i in addresses_data.values())
    _addresses_data = addresses_data

    # Scan change addresses
    print(f"\n=== Scanning change addresses ===")
    max_to_change = len(change_addrs) if is_first_run else batch_size
    checked, last_idx = scan_chain('change', change_addrs, addresses_data, wallet_addrs, addr_map, gap_threshold, _max_change, max_to_change)
    _max_change = max(_max_change, checked)
    _total_btc = sum(i.get('balance', 0) for i in addresses_data.values())

    total_with_balance = sum(i.get('balance', 0) for i in addresses_data.values())
    _addresses_data = addresses_data
    _total_btc = total_with_balance

    save_checkpoint(addresses_data, total_with_balance, xpub, _max_receive, _max_change)

    # Prompt for verification
    while True:
        is_correct, total = verify_and_prompt(addresses_data, total_with_balance, price)

        if is_correct is True:
            print("\nSaved and confirmed!")
            break
        elif is_correct is False:
            print("\nQuit without confirming.")
            break
        else:
            print("\nExtending scan by one more batch...")

            start_receive = _max_receive
            end_receive = min(len(receive_addrs), start_receive + batch_size)
            start_change = _max_change
            end_change = min(len(change_addrs), start_change + batch_size)

            print(f"\nExtra receive: indices {start_receive} to {end_receive}")
            checked, _ = scan_chain('receive', receive_addrs, addresses_data, wallet_addrs, addr_map, gap_threshold, start_receive, batch_size)
            _max_receive = max(_max_receive, checked)

            print(f"\nExtra change: indices {start_change} to {end_change}")
            checked, _ = scan_chain('change', change_addrs, addresses_data, wallet_addrs, addr_map, gap_threshold, start_change, batch_size)
            _max_change = max(_max_change, checked)

            save_checkpoint(addresses_data, total_with_balance, xpub, _max_receive, _max_change)

            total_with_balance = sum(i.get('balance', 0) for i in addresses_data.values())
            _total_btc = total_with_balance

    print(f"\nSaved to {DATA_FILE}")

################################################################################################################################
if __name__ == '__main__':
    main()
