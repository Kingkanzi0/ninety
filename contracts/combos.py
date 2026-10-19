# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

# Ninety Combos: accumulator tickets on top of the Ninety contract.
# Each leg is priced at the live Ninety pool odds when the ticket is placed and
# every leg must win. Results come from Ninety itself (read with a view call),
# so combos settle on the same validator-agreed scores. Payouts come from a house
# bankroll; a ticket is only accepted if the bankroll can cover it in full.

import json
from datetime import datetime, timezone

from genlayer import *

MIN_LEGS, MAX_LEGS = 2, 6
MAX_LEG_ODDS, MAX_ODDS = 1500, 10000     # odds x100: 15.00 per leg, 100.00 per ticket
MAX_PAYOUT = 20 * 10**18
MARKETS = ("RESULT", "GOALS", "BTTS", "SCORE")
OPEN, WON, LOST, REFUNDED = "OPEN", "WON", "LOST", "REFUNDED"
E_EXP = "[EXPECTED]"
NINETY = "0xD4807A5EBeBc9fb8aD071364Db452dd809a09129"   # the Ninety contract this reads results from


def _now():
	return int(datetime.now(timezone.utc).timestamp())


def _fail(text):
	raise gl.vm.UserError(f"{E_EXP} {text}")


def parse_legs(text):
	"""'fid:MARKET:OUTCOME,...' -> [(fid, market, outcome)], validated."""
	legs, seen = [], set()
	for part in [p.strip() for p in text.split(",") if p.strip()]:
		bits = part.split(":")
		if len(bits) != 3 or not bits[0].isdigit():
			_fail("each leg must look like 3:RESULT:HOME")
		fid, m, o = int(bits[0]), bits[1].strip().upper(), bits[2].strip().upper()
		if m not in MARKETS:
			_fail("market must be RESULT, GOALS, BTTS or SCORE")
		if fid in seen:
			_fail("one leg per match")
		seen.add(fid)
		legs.append((fid, m, o))
	if not MIN_LEGS <= len(legs) <= MAX_LEGS:
		_fail(f"a combo needs {MIN_LEGS} to {MAX_LEGS} legs")
	return legs


def leg_odds(fixture, m, o):
	"""Live pool odds x100 for one outcome of a Ninety market."""
	mk = fixture["markets"][m]
	total, pool = int(mk["total"]), int(mk["pools"].get(o, "0"))
	if pool == 0:
		_fail(f"no odds yet for {m} {o}")
	odds = total * 100 // pool
	if odds <= 100:
		_fail(f"{m} {o} has no price yet (only that side is backed)")
	return min(odds, MAX_LEG_ODDS)


def leg_result(fixture, m, o):
	"""WIN, LOSE, VOID, or PENDING for one leg."""
	if fixture["status"] == "VOID":
		return "VOID"
	if fixture["status"] != "FINAL":
		return "PENDING"
	win = fixture["markets"][m]["winner"]
	if win == "VOID":
		return "VOID"
	return "WIN" if win == o else "LOSE"


@gl.evm.contract_interface
class _Payee:
	class View:
		pass

	class Write:
		pass


class NinetyCombos(gl.Contract):
	core: str
	owner: str
	house_free: u256
	house_locked: u256
	count: u256
	t_owner: TreeMap[u256, str]
	t_legs: TreeMap[u256, str]       # json [[fid, market, outcome, odds100], ...]
	t_stake: TreeMap[u256, u256]
	t_payout: TreeMap[u256, u256]    # maximum payout reserved for the ticket
	t_status: TreeMap[u256, str]
	t_paid: TreeMap[u256, u256]
	t_placed: TreeMap[u256, u256]
	by_owner: TreeMap[str, str]      # addr -> comma list of ticket ids

	def __init__(self):
		self.core = NINETY
		self.owner = gl.message.sender_address.as_hex.lower()
		self.count = u256(0)

	def _fixture(self, fid) -> dict:
		return json.loads(gl.get_contract_at(Address(self.core)).view().get_fixture(int(fid)))

	@gl.public.write.payable
	def fund(self) -> None:
		# Anyone can add to the house bankroll.
		if int(gl.message.value) <= 0:
			_fail("send GEN to fund the house")
		self.house_free = u256(int(self.house_free) + int(gl.message.value))

	@gl.public.write
	def withdraw(self, amount: int) -> None:
		if gl.message.sender_address.as_hex.lower() != self.owner:
			_fail("only the owner can withdraw")
		if not 0 < int(amount) <= int(self.house_free):
			_fail("amount is more than the free bankroll")
		self.house_free = u256(int(self.house_free) - int(amount))
		_Payee(gl.message.sender_address).emit_transfer(value=u256(int(amount)))

	@gl.public.write.payable
	def place(self, legs: str) -> int:
		stake = int(gl.message.value)
		if stake <= 0:
			_fail("send GEN with the combo")
		priced, odds, now = [], 100, _now()
		for fid, m, o in parse_legs(legs):
			f = self._fixture(fid)
			if f["status"] != "SCHEDULED" or now >= int(f["kickoff"]):
				_fail(f"match {fid} is closed")
			if m != "SCORE" and o not in f["markets"][m]["pools"]:
				_fail(f"{o} is not an outcome of {m}")
			lo = leg_odds(f, m, o)
			priced.append([fid, m, o, lo])
			odds = min(odds * lo // 100, MAX_ODDS)
		payout = min(stake * odds // 100, MAX_PAYOUT)
		if payout <= stake:
			_fail("combo odds are too low")
		risk = payout - stake
		if risk > int(self.house_free):
			_fail("the house cannot cover this payout yet; try a smaller stake")
		self.house_free = u256(int(self.house_free) - risk)
		self.house_locked = u256(int(self.house_locked) + payout)
		tid = u256(int(self.count))
		self.count = u256(int(tid) + 1)
		who = gl.message.sender_address.as_hex.lower()
		self.t_owner[tid], self.t_legs[tid] = who, json.dumps(priced)
		self.t_stake[tid], self.t_payout[tid] = u256(stake), u256(payout)
		self.t_status[tid], self.t_placed[tid] = OPEN, u256(now)
		prev = self.by_owner.get(who, "")
		self.by_owner[who] = f"{prev},{int(tid)}" if prev else str(int(tid))
		return int(tid)

	@gl.public.write
	def settle_ticket(self, ticket_id: int) -> str:
		# Permissionless: settles as soon as one leg loses or every leg is decided.
		tid = self._tid(ticket_id)
		if self.t_status[tid] != OPEN:
			_fail("ticket is already settled")
		legs = json.loads(self.t_legs[tid])
		results = [leg_result(self._fixture(fid), m, o) for fid, m, o, _ in legs]
		stake, reserved = int(self.t_stake[tid]), int(self.t_payout[tid])
		if "LOSE" in results:
			status, pay = LOST, 0
		elif "PENDING" in results:
			_fail("legs are still being played")
		else:
			odds = 100
			for (fid, m, o, lo), r in zip(legs, results):
				if r == "WIN":
					odds = odds * lo // 100
			pay = min(stake * min(odds, MAX_ODDS) // 100, reserved)
			status = REFUNDED if all(r == "VOID" for r in results) else WON
		self.house_locked = u256(int(self.house_locked) - reserved)
		self.house_free = u256(int(self.house_free) + reserved - pay)
		self.t_status[tid], self.t_paid[tid] = status, u256(pay)
		if pay > 0:
			_Payee(Address(self.t_owner[tid])).emit_transfer(value=u256(pay))
		return status

	@gl.public.view
	def get_house(self) -> str:
		return json.dumps({"free": str(int(self.house_free)), "locked": str(int(self.house_locked)),
			"tickets": int(self.count), "core": self.core, "owner": self.owner,
			"max_payout": str(MAX_PAYOUT), "max_odds": MAX_ODDS / 100})

	@gl.public.view
	def get_ticket(self, ticket_id: int) -> str:
		return json.dumps(self._dict(self._tid(ticket_id)))

	@gl.public.view
	def get_tickets_of(self, user: str) -> str:
		ids = [int(x) for x in self.by_owner.get(Address(user).as_hex.lower(), "").split(",") if x]
		return json.dumps([self._dict(u256(i)) for i in ids[::-1][:50]])

	@gl.public.view
	def get_open_tickets(self, offset: int, limit: int) -> str:
		total, out = int(self.count), []
		for i in range(max(0, total - 1 - int(offset)), -1, -1):
			if self.t_status[u256(i)] == OPEN:
				out.append(self._dict(u256(i)))
			if len(out) >= min(int(limit), 50):
				break
		return json.dumps(out)

	def _tid(self, ticket_id) -> u256:
		if not 0 <= int(ticket_id) < int(self.count):
			_fail("unknown ticket")
		return u256(int(ticket_id))

	def _dict(self, tid) -> dict:
		return {"id": int(tid), "owner": self.t_owner[tid], "legs": json.loads(self.t_legs[tid]),
			"stake": str(int(self.t_stake[tid])), "payout": str(int(self.t_payout[tid])),
			"status": self.t_status[tid], "paid": str(int(self.t_paid.get(tid, u256(0)))),
			"placed": int(self.t_placed[tid])}
