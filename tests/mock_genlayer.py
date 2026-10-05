"""
Minimal offline stand-in for the GenLayer Python SDK, used only to unit-test
TruthBet's deterministic logic and its leader/validator comparison rules
without a network. It is NOT GenVM: real behaviour must still be confirmed on
Studio / Bradbury (see README "Testing").

What it models:
- storage types (u256, u32, TreeMap, Address) with zero defaults
- gl.message (sender, value), gl.vm.Return / UserError, gl.public decorators
- gl.nondet.web.render / gl.nondet.exec_prompt backed by per-role mocks, so the
  leader and the validators can be shown DIFFERENT web/LLM answers
- gl.vm.run_nondet_unsafe: leader runs, then each validator runs the validator
  function; if the majority disagrees the call raises ConsensusFailure
- emit_transfer on EOAs is recorded in `transfers`
"""

import sys
import types
import typing
from datetime import datetime, timezone


class u256(int):
    def __new__(cls, v=0):
        v = int(v)
        if v < 0 or v >= 2**256:
            raise OverflowError("u256 out of range")
        return super().__new__(cls, v)


class u32(int):
    def __new__(cls, v=0):
        v = int(v)
        if v < 0 or v >= 2**32:
            raise OverflowError("u32 out of range")
        return super().__new__(cls, v)


class _Generic:
    def __class_getitem__(cls, item):
        return cls


class TreeMap(dict, _Generic):
    pass


class DynArray(list, _Generic):
    pass


class Address:
    def __init__(self, v):
        if isinstance(v, Address):
            v = v.as_hex
        v = str(v)
        if not v.startswith("0x") or len(v) != 42:
            raise ValueError(f"bad address {v}")
        int(v[2:], 16)
        self._hex = v.lower()

    @property
    def as_hex(self):
        return "0x" + self._hex[2:].upper()  # stand-in for checksum casing

    def __eq__(self, o):
        return isinstance(o, Address) and o._hex == self._hex

    def __hash__(self):
        return hash(self._hex)

    def __repr__(self):
        return f"Address({self.as_hex})"


class UserError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


class Return:
    def __init__(self, calldata):
        self.calldata = calldata


class ConsensusFailure(Exception):
    """Validators rejected the leader result (would rotate / go undetermined)."""


class World:
    def __init__(self):
        self.sender = Address("0x" + "a" * 40)
        self.value = 0
        self.now = 1_800_000_000
        self.role = "leader"
        # role -> url -> text (or Exception)
        self.web = {"leader": {}, "validator": {}}
        # role -> callable(prompt) -> dict
        self.llm = {"leader": None, "validator": None}
        self.validators = 4
        self.transfers = []
        self.nondet_calls = []

    def set_web(self, url, text, role=None):
        for r in ([role] if role else ["leader", "validator"]):
            self.web[r][url] = text

    def set_llm(self, fn, role=None):
        for r in ([role] if role else ["leader", "validator"]):
            self.llm[r] = fn


world = World()


def _render(url, mode="text", **kw):
    world.nondet_calls.append(("render", world.role, url, mode))
    if mode not in ("text", "html", "screenshot"):
        raise ValueError("bad mode")
    page = world.web[world.role].get(url)
    if page is None:
        raise RuntimeError("404")
    if isinstance(page, Exception):
        raise page
    return page


def _exec_prompt(prompt, response_format=None, **kw):
    world.nondet_calls.append(("prompt", world.role, prompt[:60], response_format))
    fn = world.llm[world.role]
    return fn(prompt)


def _run_nondet_unsafe(leader_fn, validator_fn):
    world.role = "leader"
    try:
        leader_res = Return(leader_fn())
    except UserError as e:
        leader_res = e
    agree = 0
    for _ in range(world.validators):
        world.role = "validator"
        try:
            ok = bool(validator_fn(leader_res))
        except Exception:
            ok = False
        agree += 1 if ok else 0
    world.role = "leader"
    if agree * 2 <= world.validators:
        raise ConsensusFailure(f"{agree}/{world.validators} validators agreed")
    if isinstance(leader_res, UserError):
        raise leader_res
    return leader_res.calldata


def _forbidden(*a, **k):
    raise AssertionError("gl.nondet.web.get must not be used (reviewer requirement)")


def _identity_decorator(fn):
    return fn


class _Write:
    def __call__(self, fn):
        return fn

    payable = staticmethod(_identity_decorator)


def _contract_interface(cls):
    class Proxy:
        def __init__(self, addr):
            self.addr = Address(addr)

        def emit_transfer(self, value, **kw):
            world.transfers.append((self.addr.as_hex.lower(), int(value)))

    Proxy.__name__ = cls.__name__
    return Proxy


_ZERO = {u256: lambda: u256(0), u32: lambda: u32(0), str: lambda: "", bool: lambda: False,
         TreeMap: TreeMap, DynArray: DynArray, Address: lambda: Address("0x" + "0" * 40)}


class Contract:
    def __new__(cls, *a, **k):
        obj = super().__new__(cls)
        for name, typ in typing.get_type_hints(cls).items():
            typ = typing.get_origin(typ) or typ
            obj.__dict__[name] = _ZERO[typ]()
        return obj


class _Message:
    @property
    def sender_address(self):
        return world.sender

    @property
    def value(self):
        return u256(world.value)


gl = types.SimpleNamespace(
    Contract=Contract,
    message=_Message(),
    public=types.SimpleNamespace(write=_Write(), view=_identity_decorator),
    nondet=types.SimpleNamespace(
        web=types.SimpleNamespace(render=_render, get=_forbidden),
        exec_prompt=_exec_prompt,
    ),
    vm=types.SimpleNamespace(UserError=UserError, Return=Return, run_nondet_unsafe=_run_nondet_unsafe),
    evm=types.SimpleNamespace(contract_interface=_contract_interface),
    eq_principle=types.SimpleNamespace(),  # strict_eq intentionally absent: must not be used
)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(world.now, tz or timezone.utc)


def install():
    mod = types.ModuleType("genlayer")
    for name in ("gl", "u256", "u32", "TreeMap", "DynArray", "Address"):
        setattr(mod, name, globals()[name])
    mod.__all__ = ["gl", "u256", "u32", "TreeMap", "DynArray", "Address"]
    sys.modules["genlayer"] = mod
    return mod
