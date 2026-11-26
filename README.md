# Blockchain Node (Core v0.2)

Full node for the educational Bitcoin-like network.

This is the **core** that every participant runs. It maintains its own copy of the blockchain, has a wallet, can mine, talks to other peers, and registers itself with the Discovery Service.

## Features (current)

- Genesis block + persistent blockchain
- ECDSA wallet (secp256k1) with address generation
- Transaction creation + signature verification
- Proof-of-Work mining
- Difficulty is recalculated after every block using the immediately preceding block interval; peers reject blocks whose difficulty does not match the same rule
  - Difficulty is smoothly retargeted from the immediately preceding block interval. The adjustment is damped and capped at 0.25 difficulty units per block (at most 2x harder/easier per block), avoiding abrupt 16x jumps.
  - Each block stores its parent timestamp in its hashed header, so this per-block rule continues working after historical blocks are pruned.
- Longest-chain rule
- Automatic peer discovery via the Discovery Service
- Basic P2P block & transaction propagation
- REST API for chain, wallet, sending, mining
- Built-in browser dashboard with wallet overview, send form, mining control, recent-block explorer, peer count and mempool

## Quick Start

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env:
#   - Set DISCOVERY_URL to your discovery service
#   - Set NETWORK_SECRET (must match discovery)
#   - On Render, PUBLIC_URL is detected automatically from RENDER_EXTERNAL_URL
#   - On other hosts, set PUBLIC_URL to this node's public HTTPS URL

python -m app.main
```

The node will:

- Detect its public URL automatically on Render using `RENDER_EXTERNAL_URL`
- Refuse to register loopback/private peer URLs such as `127.0.0.1`

1. Create or load wallet + blockchain
2. Register with the discovery service
3. Sync with any existing peers
4. Start the HTTP API on port 8332 (default)

## Important Environment Variables

| Variable         | Description                                      |
|------------------|--------------------------------------------------|
| `DISCOVERY_URL`  | URL of the discovery service                     |
| `NETWORK_SECRET` | Shared secret (must match discovery service)     |
| `PUBLIC_URL`     | Optional public URL override; Render auto-detects `RENDER_EXTERNAL_URL` |
| `HTTP_PORT`      | Port for the API (default 8332)                  |
| `INITIAL_DIFFICULTY` | Starting difficulty units (default 1.0; fractional values supported) |
| `BLOCK_TIME_TARGET` | Target interval between blocks in seconds (default 120) |
| `BLOCK_REWARD`   | Coinbase reward (default 50)                     |

## Browser interface

Open the node's base URL (`/`) in a browser. The node serves its own responsive dashboard; no separate frontend hosting or build step is required.

The dashboard reads the node's API and refreshes automatically every 15 seconds. It includes:

- This node wallet address and confirmed balance
- Send transaction form (the existing node wallet signs the transaction)
- Mine-block control
- Recent blocks with height, hash, transaction count and timestamp
- Peer count and network status
- Pending transaction list

The original JSON API remains available. Node status is now at `/api/status`; pending transactions are at `/api/mempool`.

**Security note:** this is an experimental interface, not a production wallet. The existing `/api/send` endpoint asks the server-side node wallet to sign transactions, and the node API currently has no user authentication. Do not expose a node containing valuable keys or funds to an untrusted public network.

## Main API Endpoints

- `GET  /`                  → Browser dashboard
- `GET  /api/status`        → Node info and status
- `GET  /api/mempool`       → Pending transactions
- `GET  /api/chain`         → Full blockchain
- `GET  /api/wallet`        → This node's address + balance
- `GET  /api/balance/{addr}`→ Balance of any address
- `POST /api/send`          → Create + broadcast a transaction
- `POST /api/mine`          → Mine a block
- `GET  /api/peers`         → Currently known peers
- `POST /api/block`         → Receive a block from a peer
- `POST /api/transaction`   → Receive a transaction from a peer

## Project Structure

```
blockchain-node/
├── app/
│   ├── main.py              # Entry point
│   ├── config.py
│   ├── blockchain/
│   │   ├── models.py        # Block & Transaction
│   │   └── chain.py         # Blockchain logic
│   ├── wallet/
│   │   └── wallet.py        # Keys, signing, addresses
│   ├── network/
│   │   ├── discovery.py     # Talks to discovery service
│   │   └── p2p.py           # Peer management + sync
│   └── api/
│       └── routes.py        # FastAPI routes
├── data/                    # blockchain.json + wallet.json (auto-created)
├── static/                  # Future browser UI assets
├── templates/               # Future Jinja templates
├── requirements.txt
└── .env.example
```

## Next Steps (planned)

- Full transaction/block detail pages and address history
- Per-block dynamic difficulty adjustment (recalculated and consensus-validated for every new block)
- Cumulative chainwork
- Proper fork handling / reorgs
- Coinbase maturity enforcement
- Authenticated node-to-node communication
```


## Pruned state and recovery

The node now stores a single latest account-state snapshot outside the block. A
pruning-height block commits to the post-block account state using
`snapshot_root` and `snapshot_accounts`; the snapshot data is stored in
`data/latest_snapshot.json`. The block itself does not contain one synthetic
transaction per address.

A pruning checkpoint is a boundary in the confirmed chain state. Its snapshot
is calculated from the previous confirmed snapshot plus confirmed normal blocks
since that snapshot, including the checkpoint block's coinbase reward. The
checkpoint block deliberately contains no ordinary mempool transactions.
Unconfirmed transactions are not part of confirmed balances and are not used
when calculating the snapshot. They remain in the mempool and can be included
in the next ordinary block after the checkpoint. Transactions arriving while
proof-of-work for the checkpoint is being performed are also left pending.

After a checkpoint is accepted, the node keeps only Genesis, the latest
checkpoint, and blocks after that checkpoint. On startup and after peer sync,
older local block history is compacted away. When syncing from a pruned peer,
the node fetches that peer's latest snapshot, verifies its canonical state
hash against the checkpoint, loads it, and replays blocks after the checkpoint.
Only one current snapshot is retained; it is overwritten at the next checkpoint.
When a block is accepted, only mempool transactions actually included in that
block are removed; unrelated pending transactions are retained.

### Minimum balance in pruning snapshots

The canonical snapshot retains an address only when its balance is at least
`0.00000001` coin (one satoshi at eight-decimal precision). Zero, negative,
and sub-satoshi dust entries are omitted. This is a snapshot-state size rule;
it does not create a special transaction or a burn-address transfer.

The checkpoint block itself contains only a state commitment (hash) and the
account count, not one record per address. The balance records live in the
separate `latest_snapshot.json` file. Therefore, splitting one checkpoint into
many pruning blocks is not needed to keep the block small: its on-chain size is
independent of the number of accounts. The snapshot file can still grow with
the number of addresses holding at least one satoshi; pruning limits retained
history, not the size of the current live account state.

Sub-satoshi values are not meaningful at this prototype's eight-decimal
balance precision. The protocol should consistently use whole-satoshi amounts
when transaction amount validation is hardened; this snapshot filter alone is
not a substitute for full transaction/block validation.

Peer sync compares block height and cumulative work metadata rather than the
number of locally stored blocks. A periodic peer refresh retries synchronization
so a node that starts before peers are discoverable can catch up later.

**Checkpoint trust limitation:** the snapshot hash proves that the received
snapshot bytes match the root committed in the checkpoint. It does not, by
itself, mathematically prove that the checkpoint's balances were correctly
derived from every historical transaction. Since old blocks are deliberately
unavailable to a pruned node, this prototype's checkpoint recovery still relies
on the network's accepted proof-of-work checkpoint and peer trust. A production
network should add a robust checkpoint-consensus/quorum or a succinct
state-transition proof before treating untrusted snapshot providers as safe.
The same limitation applies to cumulative-work metadata across a pruned gap.

## Optional full-history archive copy (local-only)

Set `ARCHIVE_HISTORY=true` in `.env` to keep an additional append-only JSONL
copy of blocks observed by this node at `data/archive_history.jsonl`. This is
an optional storage-heavy archive/export copy, **not** part of the node's
consensus database and **never** used for P2P synchronization. It is not served
as a peer chain and there is no archive-import/sync endpoint.

- `GET /api/archive/status` reports whether the local archive is present and
  whether it is known to be complete from Genesis.
- `GET /api/archive/download` downloads the local JSONL copy.
- If the node first joins after the network has pruned old blocks, its archive
  cannot contain the missing history and is marked incomplete. It can only
  archive blocks it observes from that point forward.
- Disable the option to stop adding to the archive. Deleting the archive file
  does not affect node operation or synchronization.

Keep `ARCHIVE_HISTORY=false` for ordinary nodes that prioritize bounded
historical storage.
