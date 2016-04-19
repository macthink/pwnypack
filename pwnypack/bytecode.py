"""
The bytecode module lets you manipulate python bytecode in a
version-independent way. To facilitate this, this module provides a couple
of function to disassemble and assemble python bytecode into a high-level
representation and some functions to manipulate those structures.

The python version independent function take a py_internals parameter which
represents the specifics of bytecode on that particular version of
python. The :data:`pwnypack.py_internals.PY_INTERNALS` dictionary provides
these internal specifics for various python versions.

Examples:
    Disassemble a very simple function, change an opcode and reassemble it:

    >>> from pwny import *
    >>> import six
    >>> def foo(a):
    >>>     return a - 1
    ...
    >>> print(foo, six.get_function_code(foo).co_code, foo(5))
    <function foo at 0x10590ba60> b'|\x00\x00d\x01\x00\x18S' 4
    >>> ops = bc.disassemble(foo)
    >>> print(ops)
    [LOAD_FAST 0, LOAD_CONST 1, BINARY_SUBTRACT, RETURN_VALUE]
    >>> ops[2].name = 'BINARY_ADD'
    >>> print(ops)
    [LOAD_FAST 0, LOAD_CONST 1, BINARY_ADD, RETURN_VALUE]
    >>> bar = bc.rebuild_func_from_ops(foo, ops, co_name='bar')
    >>> print(bar, six.get_function_code(bar).co_code, bar(5))
    <function bar at 0x10590bb70> b'|\x00\x00d\x01\x00\x17S' 6
"""

from __future__ import print_function

import inspect
import types

import six

import pwnypack.py_internals


__all__ = ['Op', 'Label', 'disassemble', 'assemble', 'blocks_from_ops', 'calculate_max_stack_depth', 'CodeObject']


class Label(object):
    """
    Used to define a label in a series of opcodes.
    """


class Op(object):
    """
    Describe a single bytecode operation.

    Arguments:
        name(str): The name of the opcode.
        arg: The argument of the opcode. Should be ``None`` for opcodes
            without arguments, should be a :class:`Label` for opcodes that
            define a jump, should be an ``int`` otherwise.
    """

    def __init__(self, name, arg=None):
        self.name = name  #: The name of the opcode.
        self.arg = arg  #: The opcode's argument (or ``None``).

    def __repr__(self):
        if self.arg is not None:
            return '%s %r' % (self.name, self.arg)
        else:
            return self.name


def disassemble(code, py_internals=None):
    """
    Disassemble python bytecode into a series of :class:`Op` and
    :class:`Label` instances.

    Arguments:
        code(bytes): The bytecode (a code object's ``co_code`` property). You
            can also provide a function.
        py_internals(dict): The opcode specification of the python version that
            generated ``code``. If you provide ``None``, the specs for the
            currently running python version will be used.

    Returns:
        list: A list of opcodes and labels.
    """

    if inspect.isfunction(code):
        code = six.get_function_code(code).co_code

    if py_internals is None:
        py_internals = pwnypack.py_internals.PY_INTERNALS[None]

    opname = py_internals['opname']
    hasjrel = py_internals['hasjrel']
    hasjabs = py_internals['hasjabs']
    hasjump = set(hasjrel) | set(hasjabs)

    ext_arg_name = opname[py_internals['extended_arg']]
    ext_arg = 0

    addr_labels = {}
    addr_ops = []

    code_iter = enumerate(six.iterbytes(code))
    for op_addr, op_code in code_iter:
        if op_code >= py_internals['have_argument']:
            _, a = next(code_iter)
            _, b = next(code_iter)
            arg = a + (b << 8) + ext_arg

            if op_code in hasjrel:
                arg += op_addr + 3

            if op_code in hasjump:
                arg = addr_labels.setdefault(arg, Label())
        else:
            arg = None
        ext_arg = 0

        op_name = opname[op_code]

        if op_name == ext_arg_name:
            ext_arg = arg << 16
            op = None
        else:
            op = Op(op_name, arg)

        addr_ops.append((op_addr, op))

    ops = []
    for op_addr, op in addr_ops:
        label = addr_labels.get(op_addr)
        if label is not None:
            ops.append(label)

        if op is not None:
            ops.append(op)

    return ops


def assemble(ops, py_internals=None):
    """
    Assemble a set of :class:`Op` and :class:`Label` instance back into
    bytecode.

    Arguments:
        ops(list): A list of opcodes and labels (as returned by
            :func:`disassemble`).
        py_internals: The opcode specification of the targeted python
            version. If this is ``None`` the specification of the currently
            running python version will be used.

    Returns:
        bytes: The assembled bytecode.
    """

    def encode_op(op_code, op_arg=None):
        if op_arg is None:
            return six.int2byte(op_code)
        else:
            return six.int2byte(op_code) + six.int2byte(op_arg & 255) + six.int2byte(op_arg >> 8)

    if py_internals is None:
        py_internals = pwnypack.py_internals.PY_INTERNALS[None]

    opmap = py_internals['opmap']
    hasjrel = py_internals['hasjrel']
    hasjabs = py_internals['hasjabs']
    hasjump = set(hasjrel) | set(hasjabs)
    have_argument = py_internals['have_argument']
    extended_arg = py_internals['extended_arg']

    # A bit of a chicken and egg problem: The address of a label depends on the instructions before it. However,
    # the instructions before a label might depend on the label itself: For very large functions, jumps may
    # require an EXTENDED_ARG opcode if the jump destination is far away. Which we only know when the label
    # has materialized, which means the address of the label will change on the next pass, which might mean
    # a different jump offset might become larger, etc... We run passes until no label changes address.

    output = b''
    label_address = {}
    retry = True
    while retry:
        retry = False
        output = b''
        address = 0
        for op in ops:
            if isinstance(op, Label):
                if label_address.get(op) != address:
                    retry = True
                    label_address[op] = address
                continue

            op_code = opmap[op.name]
            op_arg = op.arg

            if op_arg is None:
                if op_code >= have_argument:
                    # Sanity check.
                    raise ValueError('Opcode %s requires argument.' % op)

                # Encode a single-byte opcode.
                output += encode_op(op_code)
                address += 1
                continue

            if op_code < have_argument:
                # Sanity check.
                raise ValueError('Opcode %s should not have an argument.' % op)

            if isinstance(op_arg, Label):
                if op_code not in hasjump:
                    # Sanity check.
                    raise ValueError('Did not expect label as argument for opcode %s.' % op)

                if op_arg not in ops:
                    # Sanity check.
                    raise ValueError('Label is not part of this op list.')

                # Try to turn the label argument into an address.
                op_arg = label_address.get(op_arg)
                if op_arg is None:
                    # Label hasn't materialized yet, we'll catch it on the next pass.
                    if op_code in hasjabs and address > 65535:
                        # Educated guess that we'll need an extended arg. Might save us a pass.
                        address += 6
                    else:
                        address += 3
                    continue

                if op_code in hasjrel:
                    # Fixup address for relative jump.
                    op_arg -= address + 3
            elif op_code in hasjump:
                # Sanity check.
                raise ValueError('Expected label as argument for opcode %s.' % op)

            if op_arg >= 65536:
                # Encode the extended argument (upper 16 bit of the argument).
                output += encode_op(extended_arg, op_arg >> 16)
                address += 3
                # Adjust the argument to only contain the lower 16 bits.
                op_arg &= 65535

            # Encode the opcode and the argument.
            output += encode_op(op_code, op_arg)
            address += 3

    return output


class Block(object):
    """
    A group of python bytecode ops. Produced by :func:`blocks_from_ops`.

    Arguments:
        label(:class:`Label`): The label of this block. Will be ``None`` for
            the first block.
    """

    def __init__(self, label=None):
        self.label = label  #: The label the block represents.
        self.ops = []  #: The opcodes contained within this block.
        self.next = None  #: A pointer to the next block.


def blocks_from_ops(ops):
    """
    Group a list of :class:`Op` and :class:`Label` instances by label.

    Everytime a label is found, a new :class:`Block` is created. The resulting
    blocks are returned as a dictionary to easily access the target block of a
    jump operation. The keys of this dictionary will be the labels, the values
    will be the :class:`Block` instances. The initial block can be accessed
    by getting the ``None`` item from the dictionary.

    Arguments:
        ops(list): The list of :class:`Op` and :class:`Label` instances (as
            returned by :func:`disassemble`.

    Returns:
        dict: The resulting dictionary of blocks grouped by label.
    """

    blocks = {}
    current_block = blocks[None] = Block()
    for op in ops:
        if isinstance(op, Label):
            next_block = blocks[op] = Block(op)
            current_block.next = next_block
            current_block = next_block
            continue
        current_block.ops.append(op)
    return blocks


def calculate_max_stack_depth(ops, py_internals=None):
    """
    Calculate the maximum stack depth (and required stack size) from a series
    of :class:`Op` and :class:`Label` instances. This is required when you
    manipulate the opcodes in such a way that the stack layout might change
    and you want to re-create a working function from it.

    This is a fairly literal re-implementation of python's stackdepth and
    stackdepth_walk.

    Arguments:
        ops(list): A list of opcodes and labels (as returned by
            :func:`disassemble`).

    Returns:
        int: The calculated maximum stack depth.
    """

    blocks = blocks_from_ops(ops)

    block = blocks[None]
    while block:
        block.seen = False
        block.startdepth = -1
        block = block.next

    if py_internals is None:
        py_internals = pwnypack.py_internals.PY_INTERNALS[None]

    stackeffect = py_internals['stackeffect']
    stackeffect_traits = py_internals['stackeffect_traits']

    def walk(block=None, depth=0, max_depth=0):
        if not isinstance(block, Block):
            block = blocks[block]

        if block.seen or block.startdepth >= depth:
            return max_depth

        block.seen = True
        block.startdepth = depth

        for op in block.ops:
            effect = stackeffect[op.name]
            if callable(effect):
                effect = effect(op.arg)

            depth += effect
            if depth > max_depth:
                max_depth = depth

            op_code = py_internals['opmap'][op.name]
            if op_code in py_internals['hasjrel'] or op_code in py_internals['hasjabs']:
                target_depth = depth

                if stackeffect_traits & 1:
                    if op.name == 'FOR_ITER':
                        target_depth -= 2
                    elif op.name in ('SETUP_FINALLY', 'SETUP_EXCEPT'):
                        target_depth += 3
                        if target_depth > max_depth:
                            max_depth = target_depth
                if stackeffect_traits & 2:
                    if op.name in ('JUMP_IF_TRUE_OR_POP', 'JUMP_IF_FALSE_OR_POP'):
                        depth -= 1

                max_depth = walk(op.arg, target_depth, max_depth)
            if op.name in ('JUMP_ABSOLUTE', 'JUMP_FORWARD'):
                break

        else:
            if block.next:
                max_depth = walk(block.next, depth, max_depth)

        block.seen = False

        return max_depth

    return walk()


BORROW = object()


class CodeObject(object):
    def __init__(self, co_argcount, co_kwonlyargcount, co_nlocals, co_stacksize, co_flags, co_code, co_consts,
                 co_names, co_varnames, co_filename, co_name, co_firstlineno, co_lnotab, co_freevars, co_cellvars,
                 py_internals=None):
        self.co_argcount = co_argcount
        self.co_kwonlyargcount = co_kwonlyargcount
        self.co_nlocals = co_nlocals
        self.co_stacksize = co_stacksize
        self.co_flags = co_flags
        self.co_code = co_code
        self.co_consts = co_consts
        self.co_names = co_names
        self.co_varnames = co_varnames
        self.co_filename = co_filename
        self.co_name = co_name
        self.co_firstlineno = co_firstlineno
        self.co_lnotab = co_lnotab
        self.co_freevars = co_freevars
        self.co_cellvars = co_cellvars
        if py_internals is None:
            self.py_internals = pwnypack.py_internals.PY_INTERNALS[None]
        else:
            self.py_internals = py_internals

    @classmethod
    def from_code(cls, code, co_argcount=BORROW, co_kwonlyargcount=BORROW, co_nlocals=BORROW, co_stacksize=BORROW,
                  co_flags=BORROW, co_code=BORROW, co_consts=BORROW, co_names=BORROW, co_varnames=BORROW,
                  co_filename=BORROW, co_name=BORROW, co_firstlineno=BORROW, co_lnotab=BORROW, co_freevars=BORROW,
                  co_cellvars=BORROW):
        if six.PY2:
            co_kwonlyargcount = co_kwonlyargcount if co_kwonlyargcount is not BORROW else 0
        else:
            co_kwonlyargcount = co_kwonlyargcount if co_kwonlyargcount is not BORROW else code.co_kwonlyargcount

        return cls(
            co_argcount if co_argcount is not BORROW else code.co_argcount,
            co_kwonlyargcount,
            co_nlocals if co_nlocals is not BORROW else code.co_nlocals,
            co_stacksize if co_stacksize is not BORROW else code.co_stacksize,
            co_flags if co_flags is not BORROW else code.co_flags,
            co_code if co_code is not BORROW else code.co_code,
            co_consts if co_consts is not BORROW else code.co_consts,
            co_names if co_names is not BORROW else code.co_names,
            co_varnames if co_varnames is not BORROW else code.co_varnames,
            co_filename if co_filename is not BORROW else code.co_filename,
            co_name if co_name is not BORROW else code.co_name,
            co_firstlineno if co_firstlineno is not BORROW else code.co_firstlineno,
            co_lnotab if co_lnotab is not BORROW else code.co_lnotab,
            co_freevars if co_freevars is not BORROW else code.co_freevars,
            co_cellvars if co_cellvars is not BORROW else code.co_cellvars,
        )

    @classmethod
    def from_function(cls, f, *args, **kwargs):
        return cls.from_code(six.get_function_code(f), *args, **kwargs)

    def disassemble(self):
        return disassemble(self.co_code, self.py_internals)

    def assemble(self, ops, py_internals=None):
        if py_internals is None:
            py_internals = self.py_internals
        else:
            self.py_internals = py_internals
        self.co_code = assemble(ops, py_internals)
        self.co_stacksize = calculate_max_stack_depth(ops, py_internals)
        return self

    def to_code(self):
        if self.py_internals is not pwnypack.py_internals.PY_INTERNALS[None]:
            raise ValueError('CodeObject is not compatible with the running python internals.')

        if six.PY2:
            return types.CodeType(
                self.co_argcount, self.co_nlocals, self.co_stacksize, self.co_flags, self.co_code, self.co_consts,
                self.co_names, self.co_varnames, self.co_filename, self.co_name, self.co_firstlineno, self.co_lnotab,
                self.co_freevars, self.co_cellvars
            )
        else:
            return types.CodeType(
                self.co_argcount, self.co_kwonlyargcount, self.co_nlocals, self.co_stacksize, self.co_flags,
                self.co_code, self.co_consts, self.co_names, self.co_varnames, self.co_filename, self.co_name,
                self.co_firstlineno, self.co_lnotab, self.co_freevars, self.co_cellvars
            )

    def to_function(self):
        return types.FunctionType(self.to_code(), globals())

    def __call__(self, *args, **kwargs):
        return self.to_function()(*args, **kwargs)
