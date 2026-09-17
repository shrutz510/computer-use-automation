"""Seeded fake member data. Every name, SSN and account number here is fictional."""

import random
from dataclasses import dataclass, field
from decimal import Decimal

FIRST_NAMES = ["Alex", "Jordan", "Casey", "Riley", "Morgan", "Taylor", "Quinn", "Avery", "Rowan", "Sage"]
LAST_NAMES = ["Sample", "Testerson", "Placeholder", "Demo", "Example", "Mockley", "Fakename", "Dummy"]
FROZEN_MEMBERS = {"10013", "10037"}
ACCOUNT_TYPES = ["Savings", "Money Market", "Share Certificate"]


@dataclass
class Account:
    number: str
    type: str
    nickname: str
    balance: Decimal


@dataclass
class Member:
    member_id: str
    name: str
    ssn: str
    dob: str
    status: str
    accounts: list[Account] = field(default_factory=list)

    @property
    def eligible_for_new_account(self) -> bool:
        return self.status == "Active"


class Store:
    """In-memory member store. Resets whenever the app restarts."""

    def __init__(self, seed: int = 42) -> None:
        self._rng = random.Random(seed)
        self.members: dict[str, Member] = {}
        for n in range(10001, 10051):
            mid = str(n)
            member = Member(
                member_id=mid,
                name=f"{self._rng.choice(FIRST_NAMES)} {self._rng.choice(LAST_NAMES)}",
                # 900-series SSNs are never issued, so these can't collide with real people.
                ssn=f"900-{self._rng.randint(10, 99)}-{self._rng.randint(1000, 9999)}",
                dob=f"19{self._rng.randint(50, 99)}-{self._rng.randint(1, 12):02d}-{self._rng.randint(1, 28):02d}",
                status="Frozen" if mid in FROZEN_MEMBERS else "Active",
            )
            member.accounts.append(self._new_account(mid, "Share Savings", "Primary", self._money(50, 25000)))
            member.accounts.append(self._new_account(mid, "Checking", "Everyday", self._money(10, 8000)))
            self.members[mid] = member

    def _money(self, low: int, high: int) -> Decimal:
        return Decimal(self._rng.randint(low * 100, high * 100)) / 100

    def _new_account(self, mid: str, type_: str, nickname: str, balance: Decimal) -> Account:
        return Account(number=f"7{mid}{self._rng.randint(1000, 9999)}", type=type_, nickname=nickname, balance=balance)

    def get(self, member_id: str) -> Member | None:
        return self.members.get(member_id)

    def open_sub_account(self, member: Member, type_: str, nickname: str, deposit: Decimal) -> Account:
        account = self._new_account(member.member_id, type_, nickname, deposit)
        member.accounts.append(account)
        return account
