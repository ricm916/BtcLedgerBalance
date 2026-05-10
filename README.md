# BTC Ledger Wallet Tracker

Bitcoin wallet tracking system for Ledger hardware wallet-derived addresses. Scans the Bitcoin blockchain to find all addresses with transaction history, calculates current balances, and tracks internal wallet-to-wallet transfers.

## Overview

This tool uses a BIP84 extended public key (xpub) from a Ledger hardware wallet to derive and check all receive and change addresses. It:

- Derives 100 receive addresses and 100 change addresses from the xpub
- Checks each address against the mempool.space blockchain API
- Identifies addresses with transaction history
- Calculates current balance (received - spent)
- Tracks internal transfers between wallet addresses
- Saves progress incrementally for resumable scans

To find your xpub, see the [Ledger support article](https://support.ledger.com/article/360011069619-zd).

## Requirements

```bash
pip install -r requirements.txt
```

## Configuration

1. Copy the example configuration file:
   ```bash
   cp config.py.example config.py
   ```

2. Edit `config.py` and replace the placeholder xpub with your actual xpub:

```python
# Extended public key (BIP84) for deriving receive and change addresses
xpub = "YOUR_XPUB_HERE"

# Number of consecutive empty addresses (no transaction history) to scan before stopping
SCAN_GAP_THRESHOLD = 5

# Number of addresses to check when extending scan
SCAN_BATCH_SIZE = 20
```

### Config Parameters

- `xpub`: The BIP84 extended public key from your Ledger wallet. To find your xpub, see the [Ledger support article](https://support.ledger.com/article/360011069619-zd).
- `SCAN_GAP_THRESHOLD`: How many consecutive empty addresses to scan before stopping. Wallet addresses are derived sequentially; once you hit N empty addresses, all subsequent addresses will also be empty.
- `SCAN_BATCH_SIZE`: How many addresses to check when extending the scan (after user enters 'n' at verification prompt)

## Usage

```bash
cd LEDGER
python3 main.py
```

### First Run

1. Script derives 100 receive + 100 change addresses
2. Scans receive addresses until SCAN_GAP_THRESHOLD consecutive empty
3. Scans change addresses until SCAN_GAP_THRESHOLD consecutive empty
4. Displays addresses with balances, separated by receive/change
5. Asks: "Is this correct? (y/n/q)"
   - `y`: Save and exit
   - `n`: Extend scan by one more batch of SCAN_BATCH_SIZE receive/change addresses
   - `q`: Quit without saving

### Resume / Subsequent Runs

If ledger.json exists, the script resumes from the last check point:
- Uses batch_size instead of scanning all addresses
- Stops at gap_threshold for early termination

### Ctrl+C

Pressing Ctrl+C saves the current checkpoint and exits immediately.

## Output

The results are saved to `ledger.json`:

```json
{
  "xpub": "xpub...",
  "total_balance_btc": 2.69977050,
  "max_receive_checked": 10,
  "max_change_checked": 65,
  "addresses": {
    "bc1q...": {
      "is_change": false,
      "index": 3,
      "received": 0.001,
      "spent": 0.00005770,
      "balance": 0.00094230,
      "tx_count": 2,
      "transactions": [...]
    }
  }
}
```

## Address Data Structure

Each address contains:

| Field | Description |
|-------|-------------|
| `is_change` | True for change address, False for receive |
| `index` | Derivation index (0-99) |
| `received` | Total received in BTC |
| `spent` | Total spent in BTC |
| `balance` | Current balance (received - spent) |
| `tx_count` | Number of transactions |
| `transactions` | List of internal transfers only |

### Internal Transfers

The `transactions` array stores addresses relevant to this wallet:

```json
{
  "txid": "abc123...",
  "received": 0.5,
  "spent": 0.0,
  "inputs": [
    {"address": "bc1q...", "value": 0.5}
  ],
  "outputs": [
    {"address": "bc1q...", "value": 0.7}
  ]
}
```

- `inputs`: All sending addresses (where the BTC came from)
- `outputs`: Only addresses receiving BTC within this wallet

## How It Works

### BIP44/BIP84 Address Derivation

The tool uses bip_utils to derive BIP84 (Native SegWit) addresses:

```
m/84'/0'/0'/0/0  -> receive index 0
m/84'/0'/0'/0/1  -> receive index 1
...
m/84'/0'/0'/1/0  -> change index 0
m/84'/0'/0'/1/1  -> change index 1
...
```

### Gap Limit Algorithm

BIP44 wallets generate addresses sequentially:
- Receive: 0, 1, 2, ...
- Change: 0, 1, 2, ...

The wallet uses the next unused address. Once you find N consecutive addresses with no transaction history, all subsequent addresses will also be empty.

### API Rate Limiting

The script includes a 1.2 second delay between API calls to avoid rate limiting from mempool.space.

## Project Files

```
LEDGER/
├── main.py       # Main tracking script
├── config.py     # Configuration (xpub and settings)
├── ledger.json   # Output data (auto-generated)
└── README.md     # This file
```

## Notes

- The xpub must be a BIP84 (Native SegWit) format key
- Only tracks Bitcoin (not other cryptocurrencies)
- Balance calculation: (sum of all vout where addr) - (sum of all vin where addr)
- Uses mempool.space API for blockchain data
- Uses Kraken API for BTC/USD price
- Progress saves every 5 addresses checked
