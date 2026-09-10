# coding: utf-8
"""An order id that is an int for MiniQMT and a string for the broker.

MiniQMT's ``order_stock`` returns an int: a positive 委托编号, or ``-1`` when the
submit failed. Big QMT has no such number -- ``get_trade_detail_data`` hands
back ``m_strOrderSysID``, the broker's 合同编号, which is a string. This bridge
was returning that string straight through (issue #113), so every caller
written against MiniQMT broke, and broke quietly:

    order_id = trader.order_stock(...)
    if order_id == -1:      # never true: "-1" != -1, so a rejected order
        ...                 # reads as success
    if order_id > 0:        # TypeError: '>' not supported between str and int

``OrderId`` is both. It *is* an int, so comparisons, ``isinstance`` and
arithmetic behave the way MiniQMT callers expect; and it carries the exact
broker string on ``.order_sys_id``, so a cancel sends back what QMT issued
rather than a number we invented -- including ids that are not numeric at all.

Where the 合同编号 is a plain positive integer (the usual case) the int value is
that same number and the two representations agree exactly. Where it is not,
the int is a stable surrogate derived from the string: still positive, so
``> 0`` keeps separating success from failure, while ``str()`` and the cancel
path use the real id.
"""

import zlib


# Positive and comfortably inside 32 bits, so the value survives a trip through
# JSON, a database column, or anything else that is unhappy with big integers.
_SURROGATE_MASK = 0x3FFFFFFF

_DIGITS = frozenset("0123456789")


def _is_ascii_digits(text):
    # str.isdigit() is True for '１２３' and other unicode digit forms, which
    # int() then happily parses into something the broker never issued.
    return bool(text) and all(char in _DIGITS for char in text)


def int_value_of(order_sys_id):
    """The int an ``OrderId`` for this 合同编号 should carry.

    Returns 0 for an empty id -- there is no order, so there is no number.
    """
    text = ("" if order_sys_id is None else str(order_sys_id)).strip()
    if not text:
        return 0
    if _is_ascii_digits(text):
        value = int(text)
        if value > 0:
            return value
    # Alphanumeric 合同编号, or a leading-zero form whose int would not round
    # trip. Derive a stable surrogate instead of guessing a number: same string
    # gives the same value in any process, so a caller comparing ids across a
    # restart still matches. `or 1` keeps it strictly positive.
    return (zlib.crc32(text.encode("utf-8")) & _SURROGATE_MASK) or 1


class OrderId(int):
    """``int(order_id)`` for MiniQMT, ``str(order_id)`` for the broker."""

    # No __slots__: CPython rejects a nonempty __slots__ on an int subclass
    # (variable-length builtin), so the instance carries an ordinary __dict__.

    def __new__(cls, order_sys_id, value=None):
        text = "" if order_sys_id is None else str(order_sys_id)
        if value is None:
            value = int_value_of(text)
        obj = int.__new__(cls, value)
        obj.order_sys_id = text
        return obj

    def __str__(self):
        # int.__str__ is object.__str__ on py3, which routes back through the
        # __repr__ below; int.__repr__ is the one that prints the number.
        return self.order_sys_id or int.__repr__(self)

    def __format__(self, spec):
        # "{}".format(oid) and f"{oid}" go through __format__, not __str__, and
        # int.__format__ with an empty spec would print the surrogate. Anything
        # with an actual spec ("%05d"-style) is asking for the number.
        if not spec:
            return self.__str__()
        return int.__format__(self, spec)

    def __repr__(self):
        return "OrderId(%r, %d)" % (self.order_sys_id, int(self))


def order_sys_id_of(value):
    """Best available broker string for something a caller handed back.

    Accepts an ``OrderId``, a bare string, or the int a caller round-tripped
    through JSON and lost the string half of.
    """
    carried = getattr(value, "order_sys_id", None)
    if carried:
        return str(carried)
    if value is None:
        return ""
    return str(value)
