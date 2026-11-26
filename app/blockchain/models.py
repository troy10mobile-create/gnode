from __future__ import annotations
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime, timezone
import hashlib
import json


class Transaction(BaseModel):
    txid: str = ""
    sender: str  # address or "COINBASE"
    recipient: str
    amount: float
    timestamp: float = Field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
    signature: Optional[str] = None
    public_key: Optional[str] = None  # needed for verification

    def compute_txid(self) -> str:
        data = {
            "sender": self.sender,
            "recipient": self.recipient,
            "amount": self.amount,
            "timestamp": self.timestamp,
        }
        raw = json.dumps(data, sort_keys=True).encode()
        self.txid = hashlib.sha256(raw).hexdigest()
        return self.txid

    def to_dict(self) -> dict:
        return self.model_dump()


class Block(BaseModel):
    index: int
    timestamp: float
    # Timestamp of the parent block. Stored in each new block so per-block
    # difficulty adjustment still works after older blocks are pruned.
    previous_timestamp: Optional[float] = None
    transactions: List[Transaction] = []
    previous_hash: str
    nonce: int = 0
    difficulty: float = 4.0
    hash: str = ""
    merkle_root: str = ""
    # Present only on pruning checkpoints; commits to the post-block account state.
    snapshot_root: str = ""
    snapshot_accounts: int = 0
    cumulative_work: int = 0

    def compute_merkle_root(self) -> str:
        if not self.transactions:
            self.merkle_root = "0" * 64
            return self.merkle_root

        hashes = [tx.txid or tx.compute_txid() for tx in self.transactions]
        while len(hashes) > 1:
            if len(hashes) % 2 == 1:
                hashes.append(hashes[-1])
            new_level = []
            for i in range(0, len(hashes), 2):
                combined = hashes[i] + hashes[i + 1]
                new_level.append(hashlib.sha256(combined.encode()).hexdigest())
            hashes = new_level
        self.merkle_root = hashes[0]
        return self.merkle_root

    def compute_hash(self) -> str:
        self.compute_merkle_root()
        header = {
            "index": self.index,
            "timestamp": self.timestamp,
            "previous_hash": self.previous_hash,
            "merkle_root": self.merkle_root,
            "nonce": self.nonce,
            "difficulty": (int(self.difficulty) if float(self.difficulty).is_integer()
                           else round(float(self.difficulty), 6)),
        }
        # Omit these fields for legacy blocks so their historical hashes remain valid.
        if self.previous_timestamp is not None:
            header["previous_timestamp"] = self.previous_timestamp
        if self.snapshot_root:
            header["snapshot_root"] = self.snapshot_root
            header["snapshot_accounts"] = self.snapshot_accounts
        if self.cumulative_work:
            header["cumulative_work"] = self.cumulative_work
        raw = json.dumps(header, sort_keys=True).encode()
        self.hash = hashlib.sha256(raw).hexdigest()
        return self.hash

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "timestamp": self.timestamp,
            "previous_timestamp": self.previous_timestamp,
            "transactions": [tx.to_dict() for tx in self.transactions],
            "previous_hash": self.previous_hash,
            "nonce": self.nonce,
            "difficulty": (int(self.difficulty) if float(self.difficulty).is_integer()
                           else round(float(self.difficulty), 6)),
            "hash": self.hash,
            "merkle_root": self.merkle_root,
            "snapshot_root": self.snapshot_root,
            "snapshot_accounts": self.snapshot_accounts,
            "cumulative_work": self.cumulative_work,
        }
