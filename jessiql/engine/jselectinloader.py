from collections import abc

import collections
import itertools
import sqlalchemy as sa
import sqlalchemy.orm.strategies

from jessiql.sautil.adapt import SimpleColumnsAdapter
from jessiql.typing import SAModelOrAlias, SARowDict


# Inspired by SelectInLoader._load_for_path() , SqlAlchemy v1.4.15
# With some differences;
# * We ignore the `load_with_join` branch because we can ensure all FKs are loaded
# * We do no streaming: all parent FKs are available at once, so we don't care about `order_by`s
# * We don't use baked queries for now
# * Some customizations are marked with [CUSTOMIZED]
# * Some additions are marked with [ADDED]
class JSelectInLoader:
    def __init__(self, source_model: SAModelOrAlias, relation_property: sa.orm.RelationshipProperty, target_model: SAModelOrAlias):
        self.source_model = source_model
        self.relation_property = relation_property
        self.target_model = target_model

        self.key = relation_property.key
        self.source_mapper: sa.orm.Mapper = source_model.__mapper__
        self.target_mapper: sa.orm.Mapper = relation_property.mapper

        loader = sa.orm.strategies.SelectInLoader(relation_property, ())
        self.query_info = loader._query_info

    # Inspired by SelectInLoader._load_for_path(), part 1, SqlAlchemy v1.4.15
    def prepare_states(self, states: list[SARowDict]):
        query_info = self.query_info

        # This value is set to `True` when either `relationship(omit_join=False)` was set, or when the left mapper entities
        # do not have FK keys loaded. We do not want these complications since we control how things are loaded.
        # SelectInLoader makes a larger JOIN query in such a case. We don't want that.
        assert not query_info.load_with_join

        # Okay, the `load_with_join` case is excluded.
        # The next thing that controls how a query should be built is `query_info.load_only_child`:
        # it's set to `True` for MANYTOONE relationships, and is `False` for other relationships: ONETOMANY and MANYTOMANY
        assert isinstance(query_info.load_only_child, bool)

        # This is the case of MANYTOONE.
        # This means that we have a list of entities where a foreign key is present within the result set.
        # We need to collect these foreign key columns.
        # Example:
        #   Article.users:
        #       we have a list of `Article[]` where `Article.user_id` is loaded
        #       we'll need to load `User[]` where `User.id = Article.user_id`
        if query_info.load_only_child:
            self.our_states = collections.defaultdict(list)
            self.none_states = []

            for state_dict in states:
                related_ident = tuple(
                    state_dict[lk.key]
                    for lk in query_info.child_lookup_cols
                )

                # organize states into lists keyed to particular foreign key values.
                if None not in related_ident:
                    self.our_states[related_ident].append(state_dict)
                else:
                    # For FK values that have None, add them to a separate collection that will be populated separately
                    self.none_states.append(state_dict)

        # This is the case of ONETOMANY and MANYTOMANY.
        # This means that we only have our primary key here, and the foreign key in in that other table.
        # We need to collect our primary keys.
        # Example:
        #   User.articles:
        #       we have a list of `User[]` where `User.id` is loaded
        #       we'll need to load `Article[]` where `Article.user_id = User.id`
        if not query_info.load_only_child:
            # If it fails to find a column in `state`, it means the `state` does not have a primary key loaded
            self.our_states = [
                (get_primary_key_tuple(self.source_mapper, state), state)
                for state in states
            ]

    # Inspired by SelectInLoader._load_for_path(), part 2, SqlAlchemy v1.4.15
    def prepare_query(self, q: sa.sql.Select) -> sa.sql.Select:
        # [ADDED] Adapt pk_cols
        adapter = SimpleColumnsAdapter(self.target_model)
        pk_cols = adapter.replace_many(self.query_info.pk_cols)

        q = q.add_columns(*pk_cols)  # [CUSTOMIZED]

        q = q.filter(
            adapter.replace(  # [ADDED] adapter
                self.query_info.in_expr.in_(sa.sql.bindparam("primary_keys"))
            )
        )

        return q

    def fetch_results_and_populate_states(self, connection: sa.engine.Connection, q: sa.sql.Select) -> abc.Iterator[SARowDict]:
        if self.query_info.load_only_child:
            yield from self._load_via_child(connection, self.our_states, self.none_states, q)
        else:
            yield from self._load_via_parent(connection, self.our_states, q)

    CHUNKSIZE = sa.orm.strategies.SelectInLoader._chunksize

    # Inspired by SelectInLoader._load_via_parent() , SqlAlchemy v1.4.15
    def _load_via_parent(self, connection: sa.engine.Connection, our_states: list[SARowDict], q: sa.sql.Select) -> abc.Iterator[SARowDict]:
        uselist: bool = self.relation_property.uselist
        _empty_result = lambda: [] if uselist else None

        while our_states:
            chunk = our_states[0: self.CHUNKSIZE]
            our_states = our_states[self.CHUNKSIZE:]

            primary_keys = [
                key[0] if self.query_info.zero_idx else key
                for key, state_dict in chunk
            ]

            data = collections.defaultdict(list)
            for k, v in itertools.groupby(
                    # [CUSTOMIZED]
                    connection.execute(q, {"primary_keys": primary_keys}).mappings(),#.unique()
                    lambda row: get_foreign_key_tuple(row, self.query_info),
            ):
                data[k].extend(
                    map(dict, v)  # [CUSTOMIZED] convert MappingResult to an actual, mutable dict() to which we'll add keys
                )

            for key, state_dict in chunk:
                collection = data.get(key, _empty_result())

                if not uselist and collection:
                    if len(collection) > 1:
                        sa.util.warn(f"Multiple rows returned with uselist=False for attribute {self.relation_property}")
                    state_dict[self.key] = collection[0]  # [CUSTOMIZED]
                else:
                    state_dict[self.key] = collection  # [CUSTOMIZED]

                # [ADDED] Return loaded objects
                yield from collection

    # Inspired by SelectInLoader._load_via_child() , SqlAlchemy v1.4.15
    def _load_via_child(self, connection: sa.engine.Connection, our_states: dict[tuple, list], none_states: list[dict], q: sa.sql.Select) -> abc.Iterator[SARowDict]:
        uselist: bool = self.relation_property.uselist

        # this sort is really for the benefit of the unit tests
        our_keys = sorted(our_states)
        while our_keys:
            chunk = our_keys[0: self.CHUNKSIZE]
            our_keys = our_keys[self.CHUNKSIZE:]

            data = {
                get_primary_key_tuple(self.target_mapper, row): dict(row)  # [CUSTOMIZED] Convert mappings into mutable dicts
                for row in connection.execute(q, {"primary_keys": [
                    key[0] if self.query_info.zero_idx else key
                    for key in chunk
                ]}).mappings()
            }

            for key in chunk:
                related_obj = data.get(key, None)
                for state_dict in our_states[key]:
                    state_dict[self.key] = related_obj if not uselist else [related_obj]

            # populate none states with empty value / collection
            for state_dict in none_states:
                state_dict[self.key] = None

            # [ADDED] Return loaded objects
            yield from data.values()



def get_primary_key_tuple(mapper: sa.orm.Mapper, row: SARowDict) -> tuple:
    """ Get the primary key tuple from a row dict

    Args:
        mapper: the Mapper to get the primary key from
        row: the dict to pluck from
    """
    return tuple(row[col.key] for col in mapper.primary_key)


def get_foreign_key_tuple(row: SARowDict, query_info: sa.orm.strategies.SelectInLoader.query_info) -> tuple:
    """ Get the foreign key tuple from a row dict

    Args:
        row: the dict to pluck from
        query_info: SqlALchemy SelectInLoader.query_info object that contains the necessary information
    """
    return tuple(row[col.key] for col in query_info.pk_cols)