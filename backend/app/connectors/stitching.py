"""Identity stitching: map each source's notion of "customer" onto one FeatureLedger account.

Order of evidence (first hit wins):
  1. an explicit identity already recorded in `account_identities` (system, external_id)
  2. Stripe only: the account's canonical `external_ref` is the Stripe customer id
  3. email/web domain: a UNIQUE match on `accounts.email_domain` (free-mail domains never match;
     two accounts sharing a domain is ambiguous and is not guessed)
Anything else is unresolved: usage is booked to the synthetic `__unresolved__` account and shown as a
data gap. A domain match is recorded as an identity, so the next lookup is an exact one.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .scrub import FREE_MAIL, norm_domain

UNRESOLVED_REF = "__unresolved__"


@dataclass
class AccountIndex:
    by_ref: dict[str, str] = field(default_factory=dict)                     # external_ref -> account id
    by_identity: dict[tuple[str, str], str] = field(default_factory=dict)    # (system, external id) -> account id
    by_domain: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def add_account(self, account_id: str, external_ref: str, email_domain: str | None) -> None:
        self.by_ref[external_ref] = account_id
        d = norm_domain(email_domain)
        if d and d not in FREE_MAIL:
            self.by_domain[d].add(account_id)

    def add_identity(self, system: str, external_id: str, account_id: str) -> None:
        self.by_identity[(system, str(external_id))] = account_id


@dataclass(frozen=True)
class Match:
    account_id: str | None
    method: str   # identity | stripe_ref | domain | ambiguous_domain | unresolved


def resolve(index: AccountIndex, system: str, external_id: str, domains: list[str] | None = None) -> Match:
    hit = index.by_identity.get((system, str(external_id)))
    if hit:
        return Match(hit, "identity")
    if system == "stripe" and external_id in index.by_ref:
        return Match(index.by_ref[external_id], "stripe_ref")
    candidates: set[str] = set()
    for d in domains or []:
        nd = norm_domain(d)
        if nd and nd not in FREE_MAIL:
            candidates |= index.by_domain.get(nd, set())
    if len(candidates) == 1:
        return Match(next(iter(candidates)), "domain")
    if len(candidates) > 1:
        return Match(None, "ambiguous_domain")
    return Match(None, "unresolved")
