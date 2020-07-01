"""
This analysis is meant to run on the raw data and produce a JSON represenation of a minimal
inferred API.
"""
from __future__ import annotations
import functools
import operator
import typing

import libcst as cst
import networkx
import orjson
import pydantic

from .type_analysis import *

__all__ = ["API", "Module", "Class", "Signature", "Metadata"]
Type = OutputType


def orjson_dumps(v, *, default):
    # orjson.dumps returns bytes, to match standard json.dumps we need to decode
    return orjson.dumps(v, default=default, option=orjson.OPT_INDENT_2).decode()  # type: ignore


Metadata = typing.Dict[str, int]


class API(pydantic.BaseModel):
    # Dotted module name to module
    modules: typing.Dict[str, Module] = pydantic.Field(default_factory=dict)

    class Config:
        json_loads = orjson.loads
        json_dumps = orjson_dumps

    def __ior__(self, other: API) -> API:
        update_ior(self.modules, other.modules)
        return self

    def json(self, **kwargs) -> str:
        return super().json(exclude_none=True, **kwargs)


class Module(pydantic.BaseModel):
    functions: typing.Dict[str, Signature] = pydantic.Field(default_factory=dict)
    classes: typing.Dict[str, Class] = pydantic.Field(default_factory=dict)
    properties: typing.Dict[str, typing.Tuple[Metadata, Type]] = pydantic.Field(
        default_factory=dict
    )

    @property
    def source(self) -> str:
        module = cst.Module(list(self.body))
        return module.code

    @property
    def body(
        self,
    ) -> typing.Iterable[
        typing.Union[cst.BaseCompoundStatement, cst.SimpleStatementLine]
    ]:
        yield cst.SimpleStatementLine(
            [cst.ImportFrom(cst.Name("typing"), names=cst.ImportStar())]
        )
        yield from assign_properties(self.properties)

        for name, sig in self.functions.items():
            yield sig.function_def(name)
        for name, class_ in self.classes.items():
            yield class_.class_def(name)

    def __ior__(self, other: Module) -> Module:
        update_ior(self.classes, other.classes)
        update_ior(self.functions, other.functions)

        update_metadata_and_types(self.properties, other.properties)
        # properties are union of properties, minus any things that are already classes/functins
        remove_keys(self.properties, self.classes.keys())
        remove_keys(self.properties, self.functions.keys())
        return self


class Class(pydantic.BaseModel):
    constructor: typing.Union[Signature, None] = None
    methods: typing.Dict[str, Signature] = pydantic.Field(default_factory=dict)
    classmethods: typing.Dict[str, Signature] = pydantic.Field(default_factory=dict)
    properties: typing.Dict[str, typing.Tuple[Metadata, Type]] = pydantic.Field(
        default_factory=dict
    )
    classproperties: typing.Dict[str, typing.Tuple[Metadata, Type]] = pydantic.Field(
        default_factory=dict
    )

    def class_def(self, name: str) -> cst.ClassDef:
        return cst.ClassDef(cst.Name(name), cst.IndentedBlock(list(self.body)),)

    @property
    def body(self) -> typing.Iterable[cst.BaseStatement]:
        if self.constructor is not None:
            yield self.constructor.function_def("__init__")
        yield from assign_properties(self.classproperties, True)

        for name, sig in self.classmethods.items():
            yield sig.function_def(name, is_classmethod=True)

        yield from assign_properties(self.properties)

        for name, sig in self.methods.items():
            # copy and add self as first arg
            sig = sig.copy()
            old_pos_only_required = sig.pos_only_required
            sig.pos_only_required = {"self": BottomOutput()}
            for k, v in old_pos_only_required.items():
                sig.pos_only_required[k] = v
            yield sig.function_def(name)

    def __ior__(self, other: Class) -> Class:
        if self.constructor and other.constructor:
            self.constructor |= other.constructor
        else:
            self.constructor = other.constructor
        update_ior(self.methods, other.methods)
        update_ior(self.classmethods, other.classmethods)

        update_metadata_and_types(self.classproperties, other.classproperties)
        remove_keys(self.classproperties, self.methods.keys())
        remove_keys(self.classproperties, self.classmethods.keys())

        update_metadata_and_types(self.properties, other.properties)
        remove_keys(self.properties, self.methods.keys())
        remove_keys(self.properties, self.classmethods.keys())
        # TODO: merge metadata before deleting
        # Anything that is both a class property and a property should be only a class property
        remove_keys(self.properties, self.classproperties.keys())

        return self


def assign_properties(
    p: typing.Dict[str, typing.Tuple[Metadata, Type]], is_classvar=False
) -> typing.Iterable[cst.SimpleStatementLine]:
    for name, metadata_and_tp in p.items():
        metadata, tp = metadata_and_tp
        ann = tp.annotation
        yield cst.SimpleStatementLine(
            [
                cst.AnnAssign(
                    cst.Name(name),
                    cst.Annotation(
                        cst.Subscript(
                            cst.Name("ClassVar"), [cst.SubscriptElement(cst.Index(ann))]
                        )
                        if is_classvar
                        else ann
                    ),
                )
            ],
            leading_lines=[cst.EmptyLine()]
            + [
                cst.EmptyLine(comment=cst.Comment("# " + l))
                for l in metadata_lines(metadata)
            ],
        )


PartialKeyOrdering = typing.List[typing.Tuple[str, str]]


class Signature(pydantic.BaseModel):
    # See for a helpful spec https://www.python.org/dev/peps/pep-0570/#syntax-and-semantics
    # Also keyword only PEP https://www.python.org/dev/peps/pep-3102/

    pos_only_required: typing.Dict[str, Type] = pydantic.Field(default_factory=dict)
    pos_only_optional: typing.Dict[str, Type] = pydantic.Field(default_factory=dict)
    # If there are any pos_only_optional, then there cannot be any required pos_or_kw
    pos_only_optional_ordering: PartialKeyOrdering = pydantic.Field(
        default_factory=list
    )

    pos_or_kw_required: typing.Dict[str, Type] = pydantic.Field(default_factory=dict)
    pos_or_kw_optional: typing.Dict[str, Type] = pydantic.Field(default_factory=dict)
    # Partial ordering of args, (pred, suc) pairs
    pos_or_kw_optional_ordering: PartialKeyOrdering = pydantic.Field(
        default_factory=list
    )
    # Variable args are allowed if it this is not none
    var_pos: typing.Optional[typing.Tuple[str, Type]] = None

    kw_only_required: typing.Dict[str, Type] = pydantic.Field(default_factory=dict)
    kw_only_optional: typing.Dict[str, Type] = pydantic.Field(default_factory=dict)

    # Variable kwargs are allowed if this is not none
    var_kw: typing.Optional[typing.Tuple[str, Type]] = None

    metadata: typing.Dict[str, int] = pydantic.Field(default_factory=dict)

    def validate_keys_unique(self) -> None:
        all_keys = [
            *self.pos_only_required.keys(),
            *self.pos_only_optional.keys(),
            *self.pos_or_kw_required.keys(),
            *self.pos_or_kw_optional.keys(),
            *self.kw_only_required.keys(),
            *self.kw_only_optional.keys(),
        ]
        if self.var_pos:
            all_keys.append(self.var_pos[0])
        if self.var_kw:
            all_keys.append(self.var_kw[0])

        if len(all_keys) != len(set(all_keys)):
            import pudb

            pudb.set_trace()

    def function_def(self, name: str, is_classmethod=False) -> cst.FunctionDef:
        return cst.FunctionDef(
            cst.Name(name),
            self.parameters,
            cst.IndentedBlock([cst.SimpleStatementLine([s]) for s in self.body()]),
            [cst.Decorator(cst.Name("classmethod"))] if is_classmethod else [],
        )

    def body(self) -> typing.Iterable[cst.BaseSmallStatement]:
        yield cst.Expr(self.docstring)
        yield cst.Expr(cst.Ellipsis())

    @property
    def docstring(self) -> cst.BaseExpression:
        return cst.SimpleString(
            "\n    " + "\n    ".join(metadata_lines(self.metadata)) + "\n    "
        )

    @property
    def parameters(self) -> cst.Parameters:
        return cst.Parameters(
            posonly_params=[
                cst.Param(cst.Name(k), cst.Annotation(v.annotation))
                for k, v in self.pos_only_required.items()
            ]
            + [
                cst.Param(
                    cst.Name(k), cst.Annotation(v.annotation), default=cst.Ellipsis()
                )
                for k, v in possibly_order_dict(
                    self.pos_only_optional, self.pos_only_optional_ordering
                ).items()
            ],
            params=[
                cst.Param(cst.Name(k), cst.Annotation(v.annotation))
                for k, v in self.pos_or_kw_required.items()
            ]
            + [
                cst.Param(
                    cst.Name(k), cst.Annotation(v.annotation), default=cst.Ellipsis()
                )
                for k, v in possibly_order_dict(
                    self.pos_or_kw_optional, self.pos_or_kw_optional_ordering
                ).items()
            ],
            star_arg=(
                cst.Param(
                    cst.Name(self.var_pos[0]),
                    cst.Annotation(self.var_pos[1].annotation),
                )
                if self.var_pos
                else cst.MaybeSentinel.DEFAULT
            ),
            star_kwarg=(
                cst.Param(
                    cst.Name(self.var_kw[0]), cst.Annotation(self.var_kw[1].annotation)
                )
                if self.var_kw
                else None
            ),
            kwonly_params=[
                cst.Param(cst.Name(k), cst.Annotation(v.annotation))
                for k, v in self.kw_only_required.items()
            ]
            + [
                cst.Param(
                    cst.Name(k), cst.Annotation(v.annotation), default=cst.Ellipsis()
                )
                for k, v in self.kw_only_optional.items()
            ],
        )

    @property
    def initial_args(self) -> typing.Iterator[Type]:
        """
        Iterates through default args
        """
        yield from self.pos_only_required.values()
        yield from self.pos_or_kw_required.values()
        yield from self.kw_only_required.values()

    @classmethod
    def from_params(
        cls, args: typing.List[object] = [], kwargs: typing.Dict[str, object] = {}
    ) -> Signature:
        # If we don't know what the args/kwargs are, assume the args are positional only
        # and the kwargs and keyword only
        return cls(
            pos_only_required={f"_{i}": create_type(v) for i, v in enumerate(args)},
            kw_only_required={k: create_type(v) for k, v in kwargs.items()},
        )

    @classmethod
    def from_bound_params(
        cls,
        pos_only: typing.List[typing.Tuple[str, object]] = [],
        pos_or_kw: typing.List[typing.Tuple[str, object]] = [],
        var_pos: typing.Optional[typing.Tuple[str, typing.List[object]]] = None,
        kw_only: typing.Dict[str, object] = {},
        var_kw: typing.Optional[typing.Tuple[str, typing.Dict[str, object]]] = None,
    ) -> Signature:
        return cls(
            pos_only_required={k: create_type(v) for k, v in pos_only},
            pos_or_kw_required={k: create_type(v) for k, v in pos_or_kw},
            var_pos=(
                (var_pos[0], unify(map(create_type, var_pos[1]))) if var_pos else None
            ),
            kw_only_required={k: create_type(v) for k, v in kw_only.items()},
            var_kw=(
                (var_kw[0], unify(map(create_type, var_kw[1].values())))
                if var_kw
                else None
            ),
        )

    def __ior__(self, other: Signature) -> Signature:
        self.validate_keys_unique()
        other.validate_keys_unique()

        self._copy_pos_only(other)
        self._copy_pos_or_kw(other)
        self._copy_var_pos(other)
        self._copy_kw_only(other)
        self._copy_var_kw(other)

        self.validate_keys_unique()

        update_add(self.metadata, other.metadata)
        return self

    def _copy_pos_only(self, other: Signature) -> None:
        pos_only_required = dict(
            map(
                unify_named_types,
                zip(self.pos_only_required.items(), other.pos_only_required.items()),
            )
        )
        n_pos_only_required = len(pos_only_required)
        self_new_pos_only_optional = slice_dict(
            self.pos_only_required, n_pos_only_required
        )
        other_new_pos_only_optional = slice_dict(
            other.pos_only_required, n_pos_only_required
        )
        self.pos_only_required = pos_only_required

        addititional_ordering: PartialKeyOrdering = []
        # also add to ordering that new optional keys must come before old ones, because
        # they used to be required
        if self_new_pos_only_optional and self.pos_only_optional:
            addititional_ordering.append(
                (
                    # last of required is before first of optional
                    next(iter(reversed(self_new_pos_only_optional.keys()))),
                    next(iter(self.pos_only_optional.keys())),
                )
            )
        if other_new_pos_only_optional and other.pos_only_optional:
            addititional_ordering.append(
                (
                    next(iter(reversed(other_new_pos_only_optional.keys()))),
                    next(iter(other.pos_only_optional.keys())),
                )
            )
        update_unify(
            self.pos_only_optional,
            self_new_pos_only_optional,
            other.pos_only_optional,
            other_new_pos_only_optional,
        )

        self.pos_only_optional_ordering += (
            other.pos_only_optional_ordering
            + partial_key_ordering(self_new_pos_only_optional)
            + partial_key_ordering(other_new_pos_only_optional)
            + addititional_ordering
        )

    def _copy_pos_or_kw(self, other: Signature) -> None:
        # First take off new optional keys from self and other, making sure to keep order
        self_pos_or_kw_required_keys = list(self.pos_or_kw_required.keys())
        other_pos_or_kw_required_keys = list(other.pos_or_kw_required.keys())
        pos_or_kw_required_keys = set(self_pos_or_kw_required_keys) & set(
            other_pos_or_kw_required_keys
        )
        # Pop off all required keys that are not in both sets
        self_new_optional = {
            k: self.pos_or_kw_required.pop(k)
            for k in self_pos_or_kw_required_keys
            if k not in pos_or_kw_required_keys
        }
        other_new_optional = {
            k: other.pos_or_kw_required.pop(k)
            for k in other_pos_or_kw_required_keys
            if k not in pos_or_kw_required_keys
        }
        # Now we can merge the required keys
        update_unify(
            self.pos_or_kw_required, other.pos_or_kw_required,
        )

        update_unify(
            self.pos_or_kw_optional,
            other_new_optional,
            self_new_optional,
            other.pos_or_kw_optional,
        )
        self.pos_or_kw_optional_ordering += (
            other.pos_or_kw_optional_ordering
            + partial_key_ordering(self_new_optional)
            + partial_key_ordering(other_new_optional)
        )

    def _copy_var_pos(self, other: Signature) -> None:
        self.var_pos = (
            unify_named_types((self.var_pos, other.var_pos,))
            if self.var_pos and other.var_pos
            else (self.var_pos or other.var_pos)
        )

    def _copy_kw_only(self, other: Signature) -> None:
        # Move over all required keys that aren't present in both to optional
        self_kw_only_required_keys = set(self.kw_only_required.keys())
        other_kw_only_required_keys = set(other.kw_only_required.keys())
        kw_only_required_keys = self_kw_only_required_keys & other_kw_only_required_keys
        move(
            self.kw_only_optional,
            self.kw_only_required,
            self_kw_only_required_keys - kw_only_required_keys,
            lambda l, r: unify((l, r)),
        )
        move(
            self.kw_only_optional,
            other.kw_only_required,
            other_kw_only_required_keys - kw_only_required_keys,
            lambda l, r: unify((l, r)),
        )
        # merge required and optional
        update_unify(self.kw_only_required, other.kw_only_required)
        update_unify(self.kw_only_optional, other.kw_only_optional)

        # Move any kw_only to pos_or_kw that already exist there
        # (this pops up when sometimes a function is bound and sometimes it isn't, like numpy.amin in dask, not sure why this is)
        move(
            self.pos_or_kw_required,
            self.kw_only_required,
            set(self.kw_only_required) & set(self.pos_or_kw_required),
            lambda l, r: unify((l, r)),
        )
        move(
            self.pos_or_kw_optional,
            self.kw_only_optional,
            set(self.kw_only_optional) & set(self.pos_or_kw_optional),
            lambda l, r: unify((l, r)),
        )

    def _copy_var_kw(self, other: Signature) -> None:
        self.var_kw = (
            unify_named_types((self.var_kw, other.var_kw,))
            if self.var_kw and other.var_kw
            else (self.var_kw or other.var_kw)
        )


API.update_forward_refs()
Class.update_forward_refs()
Module.update_forward_refs()


def metadata_lines(m: Metadata) -> typing.Iterable[str]:
    for k, v in m.items():
        yield f"{k}: {v}"


def partial_key_ordering(d: typing.Dict[str, Type]) -> PartialKeyOrdering:
    prev_key = None
    pairs: PartialKeyOrdering = []
    for key in d.keys():
        if prev_key is not None:
            pairs.append((prev_key, key))
        prev_key = key
    return pairs


def possibly_order_dict(
    d: typing.Dict[str, Type], order: PartialKeyOrdering
) -> typing.Dict[str, Type]:
    """
    Resort dict by topographical sorting of order
    """
    return {k: d[k] for k in networkx.topological_sort(networkx.DiGraph(order))}


def unify_named_types(
    name_and_types: typing.Iterable[typing.Tuple[typing.Optional[str], Type]]
) -> typing.Tuple[str, Type]:
    """
    Verifies the names are the same and unifies the types. Returns None if no args passed in
    or all are none
    """
    names, tps = zip(*filter(lambda x: x is not None, name_and_types))  # type: ignore
    unique_names = set(filter(lambda x: x is not None, names))
    assert len(unique_names) == 1
    return unique_names.pop(), unify(tps)


K = typing.TypeVar("K")
V = typing.TypeVar("V")


class DontAdd:
    pass


DONT_ADD = DontAdd()


def slice_dict(d: typing.Dict[K, V], n: int) -> typing.Dict[K, V]:
    """
    Slices n off the front of the dict
    """
    return dict(kv for i, kv in enumerate(d.items()) if i >= n)


def remove_keys(d: typing.Dict[K, V], ks: typing.Iterable[K]) -> None:
    for k in ks:
        if k in d:
            del d[k]


def move(
    l: typing.Dict[K, V],
    r: typing.Dict[K, V],
    keys: typing.Iterable[K],
    f: typing.Callable[[V, V], V],
) -> None:
    """
    Moves keys from right to left, making sure to use original ordering in r
    """
    for k, v in r.items():
        if k not in keys:
            continue
        if k in l:
            v = f(l[k], v)
        l[k] = v

    for k in keys:
        del r[k]


def update(
    l: typing.Dict[K, V],
    *rs: typing.Dict[K, V],
    f: typing.Callable[[V, V], typing.Union[DontAdd, V]],
) -> None:
    """
    Updates the left dict with the right dict.

    On conflicting keys calls function with left and right values to return result.
    """
    for r in rs:
        for k, v in r.items():
            if k in l:
                res = f(l[k], v)
                if isinstance(res, DontAdd):
                    del l[k]
                else:
                    l[k] = res
            else:
                l[k] = v


update_ior = functools.partial(update, f=operator.ior)
update_add = functools.partial(update, f=operator.add)
update_unify = functools.partial(update, f=lambda l, r: unify((l, r)))


def _f(
    l: typing.Tuple[Metadata, OutputType], r: typing.Tuple[Metadata, OutputType]
) -> typing.Tuple[Metadata, OutputType]:
    update_add(l[0], r[0])
    return (l[0], unify((l[1], r[1])))


update_metadata_and_types = functools.partial(update, f=_f)

