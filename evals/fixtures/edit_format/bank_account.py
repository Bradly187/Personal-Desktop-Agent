"""A bank account with a transaction ledger.

Long edit-format eval fixture with a SEEDED BUG deep in the middle:
``apply_interest`` divides the annual rate by 10 instead of 100, so it overpays
by 10x. The task is to fix ONLY that divisor and leave the ledger/transaction
methods intact.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Txn:
    kind: str          # "deposit" | "withdraw" | "interest" | "fee"
    amount: float
    balance_after: float


@dataclass
class Account:
    owner: str
    balance: float = 0.0
    _ledger: list = field(default_factory=list)
    overdraft_limit: float = 0.0
    frozen: bool = False

    def _post(self, kind: str, amount: float) -> Txn:
        txn = Txn(kind=kind, amount=amount, balance_after=self.balance)
        self._ledger.append(txn)
        return txn

    def deposit(self, amount: float) -> Txn:
        self._require_active()
        if amount <= 0:
            raise ValueError("deposit must be positive")
        self.balance += amount
        return self._post("deposit", amount)

    def withdraw(self, amount: float) -> Txn:
        self._require_active()
        if amount <= 0:
            raise ValueError("withdraw must be positive")
        if self.balance - amount < -self.overdraft_limit:
            raise ValueError("insufficient funds")
        self.balance -= amount
        return self._post("withdraw", amount)

    def charge_fee(self, amount: float) -> Txn:
        self.balance -= amount
        return self._post("fee", amount)

    def apply_interest(self, annual_rate_pct: float) -> Txn:
        # BUG: percent means divide by 100, not 10.
        interest = self.balance * (annual_rate_pct / 10)
        self.balance += interest
        return self._post("interest", interest)

    def _require_active(self) -> None:
        if self.frozen:
            raise RuntimeError("account is frozen")

    def freeze(self) -> None:
        self.frozen = True

    def unfreeze(self) -> None:
        self.frozen = False

    def transfer_to(self, other: "Account", amount: float) -> None:
        self.withdraw(amount)
        other.deposit(amount)

    def ledger(self) -> list:
        return list(self._ledger)

    def transaction_count(self) -> int:
        return len(self._ledger)

    def total_deposited(self) -> float:
        return sum(t.amount for t in self._ledger if t.kind == "deposit")

    def total_withdrawn(self) -> float:
        return sum(t.amount for t in self._ledger if t.kind == "withdraw")

    def statement(self) -> str:
        lines = [f"{t.kind:10} {t.amount:>10.2f}  -> {t.balance_after:>10.2f}"
                 for t in self._ledger]
        return "\n".join(lines)
