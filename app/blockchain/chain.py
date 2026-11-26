from __future__ import annotations

import hashlib
import json
import math
import os
import time
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone

from app.blockchain.models import Block, Transaction
from app.config import (
    DATA_DIR, INITIAL_DIFFICULTY, INITIAL_BLOCK_REWARD,
    REWARD_REDUCTION_INTERVAL, REWARD_REDUCTION_FACTOR,
    BLOCK_TIME_TARGET, MIN_DIFFICULTY,
    GENESIS_TIMESTAMP, GENESIS_PREV_HASH, PRUNE_INTERVAL, ARCHIVE_HISTORY,
    MIN_SNAPSHOT_BALANCE,
)


class Blockchain:
    """Pruned full node with a single replaceable state snapshot.

    A checkpoint commits to the account state AFTER all transactions in that
    checkpoint block. The snapshot itself is stored outside the block and only
    the latest snapshot is retained. Old blocks are not needed for node sync.
    """

    def __init__(self, data_dir: Path = DATA_DIR):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.chain_file = self.data_dir / "blockchain.json"
        self.snapshot_file = self.data_dir / "latest_snapshot.json"
        self.archive_file = self.data_dir / "archive_history.jsonl"
        self.archive_meta_file = self.data_dir / "archive_history.meta.json"
        self.chain: List[Block] = []
        self.pending_transactions: List[Transaction] = []
        self.balances: Dict[str, float] = {}
        self.archive_enabled = ARCHIVE_HISTORY
        self.load()

    # ---------------------------- Persistence ----------------------------
    def load(self):
        if self.chain_file.exists():
            with self.chain_file.open("r", encoding="utf-8") as f:
                self.chain = [Block(**b) for b in json.load(f)]
            if not self.chain:
                self.create_genesis_block()
                return
            checkpoint = self.latest_checkpoint(self.chain)
            if checkpoint:
                snapshot = self._read_snapshot()
                if snapshot and self._snapshot_matches_block(snapshot, checkpoint):
                    self.balances = self._normalise_balances(snapshot["balances"])
                    pos = next(i for i, b in enumerate(self.chain) if b.index == checkpoint.index)
                    for block in self.chain[pos + 1:]:
                        self._apply_transactions(self.balances, block.transactions)
                else:
                    # Legacy PRUNE-transaction chains are migrated by replaying
                    # their embedded snapshot. New checkpoints require a file.
                    self.recalculate_balances()
            else:
                self.recalculate_balances()
            self._compact_to_latest_checkpoint()
        else:
            self.create_genesis_block()
        self._ensure_archive_metadata()

    def save(self):
        tmp = self.chain_file.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump([b.to_dict() for b in self.chain], f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.chain_file)

    def _write_snapshot(self, block: Block, balances: Dict[str, float]):
        data = {
            "format": 1,
            "height": block.index,
            "block_hash": block.hash,
            "snapshot_root": block.snapshot_root,
            "account_count": len(balances),
            "balances": self._normalise_balances(balances),
        }
        tmp = self.snapshot_file.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, sort_keys=True, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.snapshot_file)

    def _read_snapshot(self) -> Optional[dict]:
        try:
            with self.snapshot_file.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def _normalise_balances(balances: Dict[str, float]) -> Dict[str, float]:
        """Return the canonical snapshot state, retaining balances >= 1 satoshi.

        Amounts are represented to eight decimal places in this prototype. A
        positive balance below one satoshi cannot be usefully represented in a
        pruning snapshot, so it is omitted from the canonical snapshot state.
        Zero, negative, and sub-satoshi dust entries are not serialized.
        """
        result = {}
        for address, value in sorted(balances.items()):
            rounded = round(float(value), 8)
            if rounded >= MIN_SNAPSHOT_BALANCE:
                result[address] = rounded
        return result

    @staticmethod
    def snapshot_root_for(balances: Dict[str, float]) -> str:
        canonical = json.dumps(Blockchain._normalise_balances(balances), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _snapshot_matches_block(self, snapshot: dict, block: Block) -> bool:
        try:
            balances = self._normalise_balances(snapshot["balances"])
            return (
                int(snapshot["height"]) == block.index
                and snapshot["block_hash"] == block.hash
                and snapshot["snapshot_root"] == block.snapshot_root
                and int(snapshot["account_count"]) == len(balances)
                and self.snapshot_root_for(balances) == block.snapshot_root
                and len(balances) == block.snapshot_accounts
            )
        except (KeyError, TypeError, ValueError):
            return False

    def create_genesis_block(self):
        genesis = Block(index=0, timestamp=GENESIS_TIMESTAMP, transactions=[],
                        previous_hash=GENESIS_PREV_HASH, nonce=0, difficulty=INITIAL_DIFFICULTY)
        genesis.compute_hash()
        self.chain = [genesis]
        self.balances = {}
        self.save()
        self._archive_block(genesis)
        print(f"[Blockchain] Genesis block created → {genesis.hash[:16]}...")

    # ------------------------------ Archive ------------------------------
    def _ensure_archive_metadata(self):
        if not self.archive_meta_file.exists():
            meta = {"enabled": self.archive_enabled, "complete_from_genesis": False,
                    "first_height": None, "last_height": None,
                    "note": "Local archival copy only; never used for node synchronization."}
            self._write_archive_meta(meta)

    def _write_archive_meta(self, meta: dict):
        tmp = self.archive_meta_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        tmp.replace(self.archive_meta_file)

    def _archive_block(self, block: Block):
        if not self.archive_enabled:
            return
        self._ensure_archive_metadata()
        try:
            meta = json.loads(self.archive_meta_file.read_text(encoding="utf-8"))
        except Exception:
            meta = {"enabled": True, "complete_from_genesis": False, "first_height": None, "last_height": None}
        last = meta.get("last_height")
        # Idempotent append: do not duplicate a block when a broadcast is retried.
        if last is not None and block.index <= int(last):
            return
        if last is not None and block.index != int(last) + 1:
            meta["complete_from_genesis"] = False
        if last is None:
            meta["first_height"] = block.index
            meta["complete_from_genesis"] = block.index == 0
        with self.archive_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(block.to_dict(), separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        meta["enabled"] = True
        meta["last_height"] = block.index
        meta["note"] = "Export-only local copy. This file is not a valid peer-sync source."
        self._write_archive_meta(meta)

    def archive_status(self) -> dict:
        self._ensure_archive_metadata()
        try:
            meta = json.loads(self.archive_meta_file.read_text(encoding="utf-8"))
        except Exception:
            meta = {"enabled": self.archive_enabled, "complete_from_genesis": False}
        meta["file_exists"] = self.archive_file.exists()
        meta["file_size_bytes"] = self.archive_file.stat().st_size if self.archive_file.exists() else 0
        return meta

    # --------------------------- Chain properties -------------------------
    @property
    def last_block(self) -> Block:
        return self.chain[-1]

    @property
    def height(self) -> int:
        return self.last_block.index

    def get_balance(self, address: str) -> float:
        return self.balances.get(address, 0.0)

    def current_block_reward(self, block_index: int = None) -> float:
        if block_index is None:
            block_index = self.height + 1
        era = block_index // REWARD_REDUCTION_INTERVAL
        return round(INITIAL_BLOCK_REWARD * (REWARD_REDUCTION_FACTOR ** era), 8)

    def is_pruning_height(self, height: int) -> bool:
        return height > 0 and height % PRUNE_INTERVAL == 0

    def get_current_difficulty(self) -> float:
        return INITIAL_DIFFICULTY if len(self.chain) < 2 else self.last_block.difficulty

    @staticmethod
    def _difficulty_after_parent(parent: Block, prior: Optional[Block] = None) -> float:
        """Smoothly retarget after EVERY block, using the preceding interval.

        Difficulty uses hexadecimal-zero equivalents but supports fractions.
        A +0.25 change doubles expected work. Retargeting is damped and capped
        at +/-0.25 per block (at most 2x harder/easier), avoiding abrupt 16x
        jumps from the old integer-only scheme.

        previous_timestamp is carried in each block so pruning does not erase
        the interval needed for the next retarget. Genesis's historical date
        is excluded from the first adjustment.
        """
        old = float(parent.difficulty)
        if parent.index <= 1:
            return max(MIN_DIFFICULTY, round(old, 6))

        parent_time = parent.previous_timestamp
        if parent_time is None and prior is not None and prior.index + 1 == parent.index:
            parent_time = prior.timestamp
        if parent_time is None:
            return max(MIN_DIFFICULTY, round(old, 6))

        elapsed = float(parent.timestamp) - float(parent_time)
        if elapsed <= 0:
            adjustment = 0.25
        else:
            with localcontext() as ctx:
                ctx.prec = 50
                ratio = Decimal(str(BLOCK_TIME_TARGET)) / Decimal(str(elapsed))
                raw = ratio.ln() / Decimal(16).ln()
                adjustment = float(raw * Decimal("0.25"))
        adjustment = max(-0.25, min(0.25, adjustment))
        return max(MIN_DIFFICULTY, round(old + adjustment, 6))

    @staticmethod
    def meets_difficulty(block_hash: str, difficulty: float) -> bool:
        """Check proof of work against a precise 256-bit target."""
        try:
            difficulty = float(difficulty)
            if not math.isfinite(difficulty) or difficulty < MIN_DIFFICULTY:
                return False
            with localcontext() as ctx:
                ctx.prec = 100
                scale = Decimal(16) ** Decimal(str(round(difficulty, 6)))
                target = int(Decimal((1 << 256) - 1) / scale)
            return int(block_hash, 16) <= target
        except (ValueError, ArithmeticError):
            return False

    def calculate_next_difficulty(self) -> float:
        prior = self.chain[-2] if len(self.chain) >= 2 else None
        return self._difficulty_after_parent(self.last_block, prior)

    # ---------------------------- State engine ----------------------------
    @staticmethod
    def _apply_transactions(state: Dict[str, float], transactions: List[Transaction]):
        for tx in transactions:
            if tx.sender == "PRUNE":  # legacy compatibility only
                continue
            if tx.sender not in ("COINBASE", "PRUNE"):
                state[tx.sender] = state.get(tx.sender, 0.0) - float(tx.amount)
            state[tx.recipient] = state.get(tx.recipient, 0.0) + float(tx.amount)
        for addr in list(state):
            state[addr] = round(state[addr], 8)
            if abs(state[addr]) <= 1e-12:
                del state[addr]

    def recalculate_balances(self):
        self.balances = {}
        checkpoint = self.latest_checkpoint(self.chain)
        if checkpoint:
            snap = self._read_snapshot()
            if snap and self._snapshot_matches_block(snap, checkpoint):
                self.balances = self._normalise_balances(snap["balances"])
                pos = next(i for i, b in enumerate(self.chain) if b.index == checkpoint.index)
                for block in self.chain[pos + 1:]:
                    self._apply_transactions(self.balances, block.transactions)
                return
            # Backward compatibility for the previous PRUNE-transaction format.
            for tx in checkpoint.transactions:
                if tx.sender == "PRUNE":
                    self.balances[tx.recipient] = self.balances.get(tx.recipient, 0.0) + tx.amount
            pos = next(i for i, b in enumerate(self.chain) if b.index == checkpoint.index)
            for block in self.chain[pos + 1:]:
                self._apply_transactions(self.balances, block.transactions)
            return
        for block in self.chain:
            self._apply_transactions(self.balances, block.transactions)

    @staticmethod
    def latest_checkpoint(chain: List[Block]) -> Optional[Block]:
        for block in reversed(chain):
            if block.snapshot_root or any(tx.sender == "PRUNE" for tx in block.transactions):
                return block
        return None

    def add_transaction(self, tx: Transaction) -> bool:
        if not tx.txid:
            tx.compute_txid()
        if tx.sender not in ("COINBASE", "PRUNE"):
            if tx.amount <= 0 or self.get_balance(tx.sender) < tx.amount:
                return False
        if any(t.txid == tx.txid for t in self.pending_transactions):
            return False
        self.pending_transactions.append(tx)
        return True

    def _remove_included_pending(self, transactions: List[Transaction]):
        """Remove only mempool transactions confirmed by this accepted block.

        Transactions not included in the block remain pending. This matters at
        pruning checkpoints, which deliberately do not consume the mempool.
        """
        included_ids = {tx.txid for tx in transactions if tx.txid}
        if included_ids:
            self.pending_transactions = [
                tx for tx in self.pending_transactions if tx.txid not in included_ids
            ]

    # ---------------------------- Mining/pruning --------------------------
    def mine_block(self, miner_address: str) -> Optional[Block]:
        next_height = self.height + 1
        coinbase = Transaction(sender="COINBASE", recipient=miner_address,
                               amount=self.current_block_reward(next_height))
        coinbase.compute_txid()

        # A pruning checkpoint closes the confirmed-state snapshot at this
        # height. It must not pull unconfirmed mempool transactions into that
        # snapshot. Those transactions remain pending for a later normal block.
        is_checkpoint = self.is_pruning_height(next_height)
        transactions = [coinbase]
        if not is_checkpoint:
            transactions.extend(self.pending_transactions.copy())

        # Calculate the exact post-block state before committing its root.
        resulting_state = dict(self.balances)
        self._apply_transactions(resulting_state, transactions)
        snapshot_root = self.snapshot_root_for(resulting_state) if self.is_pruning_height(next_height) else ""

        next_difficulty = self.calculate_next_difficulty()
        block = Block(index=next_height, timestamp=datetime.now(timezone.utc).timestamp(),
                      previous_timestamp=self.last_block.timestamp,
                      transactions=transactions, previous_hash=self.last_block.hash,
                      difficulty=next_difficulty, snapshot_root=snapshot_root,
                      snapshot_accounts=len(resulting_state) if snapshot_root else 0,
                      cumulative_work=self.chain_work() + self.block_work_for_difficulty(next_difficulty))
        last_timestamp_refresh = time.monotonic()
        while True:
            # Keep the block timestamp close to when the proof is actually
            # found. The timestamp is part of the hash, so changing it simply
            # starts searching a new header/nonce space; nonce remains valid
            # as a search counter.
            now_mono = time.monotonic()
            if now_mono - last_timestamp_refresh >= 5:
                block.timestamp = max(
                    datetime.now(timezone.utc).timestamp(),
                    block.previous_timestamp + 0.001,
                )
                last_timestamp_refresh = now_mono
            block.compute_hash()
            if self.meets_difficulty(block.hash, block.difficulty):
                break
            block.nonce += 1

        self.chain.append(block)
        self._remove_included_pending(transactions)
        self.balances = resulting_state
        self._archive_block(block)
        if snapshot_root:
            self._write_snapshot(block, resulting_state)
            self._compact_to_latest_checkpoint()
        self.save()
        print(f"[Blockchain] Mined block #{block.index} → {block.hash[:16]}... (nonce={block.nonce})")
        return block

    def _compact_to_latest_checkpoint(self):
        checkpoint = self.latest_checkpoint(self.chain)
        if not checkpoint:
            return
        # Keep Genesis, exactly one latest checkpoint, and all blocks after it.
        kept = [self.chain[0]] + [b for b in self.chain if b.index >= checkpoint.index]
        if len(kept) != len(self.chain):
            removed = len(self.chain) - len(kept)
            self.chain = kept
            print(f"[Blockchain] Pruned {removed} historical blocks; retained checkpoint #{checkpoint.index}.")

    # -------------------------- Validation / sync -------------------------
    @staticmethod
    def block_work_for_difficulty(difficulty: float) -> int:
        with localcontext() as ctx:
            ctx.prec = 100
            return int(Decimal(16) ** Decimal(str(round(float(difficulty), 6))))

    @classmethod
    def block_work(cls, block: Block) -> int:
        return cls.block_work_for_difficulty(block.difficulty)

    def chain_work(self, chain: Optional[List[Block]] = None) -> int:
        selected = chain or self.chain
        if selected and selected[-1].cumulative_work:
            return int(selected[-1].cumulative_work)
        return sum(self.block_work(b) for b in selected if b.index > 0)

    def is_valid_chain(self, chain: List[Block] = None) -> bool:
        chain = chain or self.chain
        if not chain or chain[0].index != 0 or chain[0].previous_hash != GENESIS_PREV_HASH:
            return False
        if chain[0].compute_hash() != chain[0].hash:
            return False
        prev = chain[0]
        last_height = 0
        for position, current in enumerate(chain[1:], start=1):
            if current.index <= last_height:
                return False
            is_checkpoint = bool(current.snapshot_root) or any(tx.sender == "PRUNE" for tx in current.transactions)
            if current.index == prev.index + 1:
                if current.previous_hash != prev.hash:
                    return False
            elif not (prev.index == 0 and is_checkpoint):
                return False
            if current.index == prev.index + 1 and current.previous_timestamp is not None:
                # New-format blocks carry their parent's timestamp and are
                # checked against the per-block retarget rule. Older blocks
                # without this field remain readable during network upgrade.
                if (current.previous_timestamp != prev.timestamp
                        or current.timestamp <= prev.timestamp
                        or current.timestamp > time.time() + 7200):
                    return False
                prior = chain[position - 2] if position >= 2 else None
                expected_difficulty = self._difficulty_after_parent(prev, prior)
                if not math.isclose(float(current.difficulty), expected_difficulty, rel_tol=0.0, abs_tol=1e-6):
                    return False
            if current.compute_hash() != current.hash or not self.meets_difficulty(current.hash, current.difficulty):
                return False
            if current.cumulative_work:
                if current.index == prev.index + 1 and prev.cumulative_work:
                    if current.cumulative_work != prev.cumulative_work + self.block_work(current):
                        return False
                elif current.cumulative_work < self.block_work(current):
                    return False
            # A compacted checkpoint must be at the configured interval.
            if current.snapshot_root and not self.is_pruning_height(current.index):
                return False
            prev, last_height = current, current.index
        return True

    def apply_block(self, block: Block) -> bool:
        if block.index != self.height + 1 or block.previous_hash != self.last_block.hash:
            return False
        if block.previous_timestamp is None or block.previous_timestamp != self.last_block.timestamp:
            return False
        if block.timestamp <= self.last_block.timestamp or block.timestamp > time.time() + 7200:
            return False
        if not math.isclose(float(block.difficulty), self.calculate_next_difficulty(), rel_tol=0.0, abs_tol=1e-6):
            return False
        if block.compute_hash() != block.hash or not self.meets_difficulty(block.hash, block.difficulty):
            return False
        state = dict(self.balances)
        self._apply_transactions(state, block.transactions)
        if self.is_pruning_height(block.index):
            if not block.snapshot_root or self.snapshot_root_for(state) != block.snapshot_root:
                print(f"[Node] Rejected checkpoint #{block.index}: state commitment mismatch")
                return False
            if len(state) != block.snapshot_accounts:
                return False
        elif block.snapshot_root:
            return False
        self.chain.append(block)
        self.balances = state
        self._remove_included_pending(block.transactions)
        self._archive_block(block)
        if block.snapshot_root:
            self._write_snapshot(block, state)
            self._compact_to_latest_checkpoint()
        self.save()
        return True

    def replace_chain(self, new_chain: List[Block], snapshot: Optional[dict] = None) -> bool:
        if not new_chain:
            return False
        if not self.is_valid_chain(new_chain):
            return False
        remote_work = self.chain_work(new_chain)
        local_work = self.chain_work(self.chain)
        if remote_work <= local_work:
            return False

        checkpoint = self.latest_checkpoint(new_chain)
        new_state: Dict[str, float] = {}
        if checkpoint and checkpoint.snapshot_root:
            if snapshot is None or not self._snapshot_matches_block(snapshot, checkpoint):
                print("[Sync] Refusing checkpoint: missing or invalid state snapshot.")
                return False
            new_state = self._normalise_balances(snapshot["balances"])
            pos = next(i for i, b in enumerate(new_chain) if b.index == checkpoint.index)
            for block in new_chain[pos + 1:]:
                new_state_before = dict(new_state)
                self._apply_transactions(new_state, block.transactions)
                # A later checkpoint must commit to the state reached from the prior checkpoint.
                if block.snapshot_root:
                    if self.snapshot_root_for(new_state) != block.snapshot_root:
                        return False
                    if len(new_state) != block.snapshot_accounts:
                        return False
        else:
            # No checkpoint: only replayable chains can be adopted.
            if new_chain[0].index != 0 or any(b.index != i for i, b in enumerate(new_chain)):
                return False
            for block in new_chain[1:]:
                self._apply_transactions(new_state, block.transactions)

        if self.archive_enabled:
            self._ensure_archive_metadata()
            try:
                meta = json.loads(self.archive_meta_file.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
            archive_last = meta.get("last_height")
            if checkpoint and checkpoint.index > 0 and (archive_last is None or int(archive_last) < checkpoint.index):
                # A compact peer chain cannot refill archival history this node missed.
                meta["complete_from_genesis"] = False
                meta["archive_gap_reason"] = "Node synchronized from a pruned checkpoint; missing historical blocks are unavailable."
                self._write_archive_meta(meta)
            elif not checkpoint:
                # A peer supplied a replayable full chain: fill any gaps in the local copy.
                start_after = int(archive_last) if archive_last is not None else -1
                for archived_block in new_chain:
                    if archived_block.index > start_after:
                        self._archive_block(archived_block)
        self.chain = list(new_chain)
        self.balances = new_state
        self._compact_to_latest_checkpoint()
        latest = self.latest_checkpoint(self.chain)
        if latest and latest.snapshot_root:
            self._write_snapshot(latest, self.balances if latest.index == self.height else self._state_at_checkpoint(snapshot, new_chain, latest))
            # If blocks follow the checkpoint, persisted snapshot must be the checkpoint state,
            # not the tip state. Recompute it from the supplied base snapshot.
        self.recalculate_balances()
        self.save()
        print(f"[Blockchain] Synced. Height={self.height}, stored_blocks={len(self.chain)}")
        return True

    def _state_at_checkpoint(self, base_snapshot: dict, chain: List[Block], checkpoint: Block) -> Dict[str, float]:
        # The supplied snapshot corresponds to the latest checkpoint returned by peer.
        return self._normalise_balances(base_snapshot.get("balances", {}))

    def get_snapshot_payload(self) -> Optional[dict]:
        snap = self._read_snapshot()
        checkpoint = self.latest_checkpoint(self.chain)
        if checkpoint and checkpoint.snapshot_root and snap and self._snapshot_matches_block(snap, checkpoint):
            return snap
        return None

    def get_chain_dict(self) -> List[dict]:
        return [b.to_dict() for b in self.chain]
