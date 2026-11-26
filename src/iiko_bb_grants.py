"""Explicit, revocable iiko access for a paired BB installation."""
import hashlib
import hmac
import json
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID

from .iiko_grant_term import valid_term


class AccessDenied(Exception):
    pass


def total(review):
    try:
        rows = [Decimal(str(item["sum"])) for item in review["document"]["items"]]
        if not rows or any(not row.is_finite() or row < 0 for row in rows):
            raise ValueError
        return sum(rows, Decimal(0))
    except (InvalidOperation, KeyError, TypeError, ValueError):
        raise ValueError("Некорректная сумма в сверке") from None


class GrantStore:
    def __init__(self, root: Path):
        self.root = root

    def propose(self, ident, proposal):
        from .iiko_connector import save_json

        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (self.root / (ident + ".json")).exists():
            raise AccessDenied
        # Идентификатор привязан к первому запросу. Его нельзя незаметно заменить до подтверждения.
        path = self.root / (ident + ".pending")
        if path.exists():
            previous = self.pending(ident)
            for key in proposal:
                if key != "until" and previous[key] != proposal[key]:
                    raise AccessDenied
        save_json(path, proposal)

    def pending(self, ident):
        return json.loads((self.root / (str(UUID(ident)) + ".pending")).read_text())

    def activate(self, ident, proposal, owner):
        from .iiko_connector import save_json

        path = self.root / (ident + ".json")
        if path.exists():
            raise AccessDenied
        grant = {k: proposal[k] for k in ("digest", "project", "organizations", "ceiling")}
        grant.update(id=ident, owner=owner, expires=int(time.time()) + proposal["days"] * 86400)
        save_json(path, grant)

    def active(self, token):
        m = re.fullmatch(r"bb_iiko\.([0-9a-f-]{36})\.([A-Za-z0-9_-]{43})", token)
        if not m:
            raise AccessDenied
        try:
            ident = str(UUID(m[1]))
            grant = json.loads((self.root / (ident + ".json")).read_text())
        except (FileNotFoundError, ValueError):
            raise AccessDenied from None
        if (grant["id"] != ident or not valid_term(grant, time.time())
                or not hmac.compare_digest(grant["digest"], hashlib.sha256(m[2].encode()).hexdigest())):
            raise AccessDenied
        return grant

    def revoke(self, ident, owner):
        from .iiko_connector import save_json

        path = self.root / (str(UUID(ident)) + ".json")
        if not path.is_file():
            raise AccessDenied
        grant = json.loads(path.read_text())
        if grant["owner"] != owner:
            raise AccessDenied
        grant["revoked"] = True
        save_json(path, grant)


class ReviewAccess:
    def __init__(self, store, token, project, owner_allowed, organizations, revision=None):
        self.store, self.token, self.project = store, token, project
        self.owner_allowed, self.organizations = owner_allowed, organizations
        self.revision = revision

    def grant(self):
        g = self.store.active(self.token)
        if self.project != g["project"] or not self.owner_allowed(g["owner"]):
            raise AccessDenied
        return g

    def inspect(self, review):
        grant = self.grant()
        org = review["organizationId"]
        if org not in self.organizations or org not in grant["organizations"] or total(review) > Decimal(grant["ceiling"]):
            raise AccessDenied
        # The draft's local ID can change to the ID assigned by iiko after a successful create.
        doc = dict(review["document"])
        if review["kind"] == "draft":
            doc.pop("documentId", None)
        from .iiko_workflow import fingerprint
        digest = fingerprint([grant["id"], review["id"], review["kind"], org, doc,
                              review.get("draft"), review["text"], review["fingerprint"]])
        return grant, hmac.new(self.token.encode(), digest.encode(), hashlib.sha256).hexdigest()

    def verify(self, review):
        grant, expected = self.inspect(review)
        if not self.revision or not hmac.compare_digest(expected, self.revision):
            raise ValueError("Сверка изменилась. Откройте актуальные строки в BB")
        return dict(grant=grant["id"], owner=grant["owner"], project=self.project,
                    revision=expected, time=int(time.time()))
