import os
import pytest
import sqlalchemy as sa
import sqlalchemy.orm

import graphql
from graphql import graphql_sync
from graphql import GraphQLResolveInfo

import jessiql
from jessiql import QueryObjectDict
from jessiql.integration.graphql import query_object_for
from jessiql.testing.graphql import prepare_graphql_query_for, resolves
from jessiql.query_object import rewrite
from jessiql.util import sacompat

from tests.util.models import IdManyFieldsMixin


EMPTY_QUERY_OBJECT = dict(
    select=[],
    join={},
    sort=[],
    filter={},
    skip=None,
    limit=None,
)


def query(**fields):
    return {
        **EMPTY_QUERY_OBJECT,
        **fields
    }


@pytest.mark.parametrize(('query', 'variables', 'expected_result_query'), [
    # Test: no explicit JessiQL query
    (
        '''
        query {
            object { id query }
        }
        ''',
        {},
        {
            'object': {
                # NOTE: select[] includes "query": field query function does not inspect the model!
                # This naïve selector would include every field that it finds
                'id': '1', 'query': query(select=['id', 'query']),
            },
        }
    ),
    # Test: field aliases
    (
            '''
            query {
                first: object { first_id: id query }
                second: object { second_id: id query }
            }
            ''',
            {},
            {
                # These top-level names come from graphQL field names themselves
                'first': {
                    # The query must contain un-aliased names!
                    'first_id': '1', 'query': query(select=['id', 'query']),
                },
                'second': {
                    # The query must contain un-aliased names!
                    'second_id': '1', 'query': query(select=['id', 'query']),
                },
            }
    ),
    # Test: JessiQL query: "query" argument
    (
        '''
        query($query: QueryObjectInput) {
            objects( query: $query ) { id query }
        }
        ''',
        {'query': dict(limit=10, sort=['id-'])},
        {
            'objects': [{
                # The final query object: [ id query ] fields + "query" argument put together
                'id': '1', 'query': query(select=['id', 'query'], limit=10, sort=['id-']),
            }],
        }
    ),
    # Test: JessiQL nested query, no explicit "query" argument
    (
        '''
        query($query: QueryObjectInput) {
            objects( query: $query ) {
                id query
                objects { id }
            }
        }
        ''',
        {'query': dict(sort=['id-'])},
        {
            'objects': [{
                'id': '1', 'query': query(select=['id', 'query'], sort=['id-'], join={'objects': query(select=['id'])}),
                'objects': [{'id': '1'}],
            }]
        }
    ),
    # Test: JessiQL nested query
    (
        '''
        query($query: QueryObjectInput) {
            objects {
                id query
                objects (query: $query) { id }
            }
        }
        ''',
        {'query': dict(sort=['id-'])},
        {
            'objects': [{
                'id': '1',
                'query': query(
                    select=['id', 'query'],
                    join={
                        'objects': query(
                            select=['id'],
                            sort=['id-'])}),
                'objects': [{
                    'id': '1',
                }],
            }]
        }
    ),
    # Test: JessiQL nested query for "objects", multi-level
    (
    ''' query {
        objects {
            id query
            objects { id objects { id } }
        }
    }
    ''',
        {},
        {
            'objects': [{
                'id': '1',
                'query': query(
                    select=['id', 'query'],
                    join={
                        'objects': query(
                            select=['id'],
                            join={
                                'objects': query(
                                    select=['id'])})}),
                'objects': [{
                    'id': '1',
                    'objects': [{
                        'id': '1',
                    }]}],
            }]
        }
    ),
    # Test: JessiQL nested query for "object" (singular), multi-level
    (
        ''' query {
            object {
                id query
                object { id object { id } }
            }
        }
        ''',
            {},
            {
                'object': {
                    'id': '1',
                    'query': query(
                        select=['id', 'query'],
                        join={
                            'object': query(
                                select=['id'],
                                join={
                                    'object': query(
                                        select=['id'])})}),
                    'object': {
                        'id': '1',
                        'object': {
                            'id': '1',
                        }},
                }
            }
        ),
    # Test: does not fail when a non-null parameter is present
    # get_query_argument_name_for() used to fail because it didn't un-wrap wrapper types like NonNull
    (
        '''
        query($id: Int!, $query: QueryObjectInput) {
            getObject(id: $id, query: $query)
        }
        ''',
        {'id': 1, 'query': dict(sort=['id-'])},
        {'getObject': None},
    ),
])
def test_query_object(query: str, variables: dict, expected_result_query: dict):
    """ Test how Query Object is generated """
    # Prepare our schema
    schema = graphql.build_schema(schema_prepare())

    # GraphQL resolver
    @resolves(schema, 'Query', 'object')
    @resolves(schema, 'Model', 'object')
    def resolve_object(obj, info: GraphQLResolveInfo, query: QueryObjectDict = None):
        query_object = query_object_for(info, runtime_type='Model')
        return {
            'id': 1,
            'query': query_object.dict(),
        }

    @resolves(schema, 'Query', 'objects')
    @resolves(schema, 'Model', 'objects')
    def resolve_objects(obj, info: GraphQLResolveInfo, query: QueryObjectDict = None):
        query_object = query_object_for(info, runtime_type='Model')
        return [
            {
                'id': 1,
                'query': query_object.dict(),
            },
        ]

    @resolves(schema, 'Query', 'getObject')
    def resolve_object(obj, info: GraphQLResolveInfo, id: int, query: QueryObjectDict = None) -> int:
        # just fail in case of bugs when getting the QueryObject
        query_object = query_object_for(info, runtime_type='Model')

    # Execute
    res = graphql_sync(schema, query, variable_values=variables)

    if res.errors:
        raise res.errors[0]
    assert res.data == expected_result_query


@pytest.mark.parametrize(('query_str', 'expected_query_object'), [
    # Test: Query Object only includes real fields
    (
    '''
    query {
        object {
            # Real SA model attributes
            id a b c
            # Do not exist on the model
            x y z query
        }
    }
    ''',
    query(select=['id', 'a', 'b', 'c', 'x', 'y', 'z', 'query']),
    ),
    # Test: camelCase field conversion
    (
    '''
    query {
        object {
            id a b c
            objectId
            query
        }
    }
    ''',
    query(select=['id', 'a', 'b', 'c', 'object_id', 'query']),
    ),
    # Test: nested objects
    (
    '''
    query {
        object {
            # 'a' is real, 'z' is not
            a z
            object {
                a z
                objects {
                    a z
                }
            }
        }
    }
    ''',
    query(
        select=['a', 'z'],
        join={
            'object': query(
            select=['a', 'z'],
                join={
                    'objects': query(select=['a', 'z'])
                }
            ),
        }
    ),
    ),
])
def test_query_object_with_sa_model(query_str: str, expected_query_object: dict):
    """ Test how Query Object works with a real SqlAlchemy model """
    # Models
    Base = sacompat.declarative_base()
    class Model(IdManyFieldsMixin, Base):
        __tablename__ = 'models'

        # Define some relationships
        object_id = sa.Column(sa.ForeignKey('models.id'))
        object_ids = sa.Column(sa.ForeignKey('models.id'))

        object = sa.orm.relationship('Model', foreign_keys=object_id)
        objects = sa.orm.relationship('Model', foreign_keys=object_ids)

    # Prepare the schema and the query document
    qctx = prepare_graphql_query_for(schema_prepare(), query_str)

    # Prepare QuerySettings
    def to_snake_case(name: str) -> str:
        # We don't have ariadne here, so let's fake it
        if name == 'objectId':
            return 'object_id'
        else:
            return name

    qsets = jessiql.QuerySettings(
        rewriter=rewrite.RewriteSAModel(
            rewrite.Transform(to_snake_case),
            Model=Model,
        )
    )

    # Get the Query Object
    api_query_object = query_object_for(qctx.info, runtime_type='Model')
    query_object = qsets.rewriter.query_object(api_query_object)
    assert query_object.dict() == expected_query_object


@pytest.mark.parametrize(('query_str', 'variables', 'expected_query_object'), [
    # Test: Relay query
    (
        '''
        query {
            users { edges { node { id login email } } pageInfo }
        }
        ''',
        {},
        query(
            select=['id', 'login', 'email'],
        )
    )
])
def test_query_object_relay_pagination(query_str: str, variables: dict, expected_query_object: dict):
    """ Test how Query Object is generated for Relay pagination """
    # Prepare our schema
    from jessiql.integration.graphql.pager_relay import relay_query_object_for

    # language=graphql
    schema = ("""
        type Query {
            users(first: Int, after: String, last: Int, before: String, query: QueryObjectInput): UserConnection
        }

        type UserConnection implements Connection {
           edges: [UserEdge!]!
           pageInfo: PageInfo!
        }

        type UserEdge implements Edge {
            node: User!
            cursor: String
        }

        type User {
            id: ID
            login: String
            email: String
        }
    """
        + load_graphql_file(jessiql.integration.graphql, 'query_object.graphql')
        + load_graphql_file(jessiql.integration.graphql, 'object.graphql')
        + load_graphql_file(jessiql.integration.graphql, 'pager_relay.graphql')
    )

    # Prepare the schema and the query document
    qctx = prepare_graphql_query_for(schema, query_str)

    # Get the Query Object
    query_object = relay_query_object_for(qctx.info, runtime_type='User')
    assert query_object.dict() == expected_query_object



# language=graphql
GQL_SCHEMA = '''
type Query {
    object (query: QueryObjectInput): Model
    objects (query: QueryObjectInput): [Model]

    # Test a special use case where code failed on wrapped (NonNull) objects
    getObject(id: Int!, query: QueryObjectInput): Int
}

type Model {
    # Real Model fields
    id: ID!
    a: String!
    b: String!
    c: String!
    d: String!

    objectId: ID
    objectIds: [ID]

    # Real relationships
    object (query: QueryObjectInput): Model
    objects (query: QueryObjectInput): [Model]

    # Virtual attribute that returns the Query Object
    query: Object

    # Some virtual attributes that only exist in GraphQL
    x: String!
    y: String!
    z: String!
}
'''


def schema_prepare() -> str:
    return (
        GQL_SCHEMA +
        # Also load QueryObject and QueryObjectInput
        load_graphql_file(jessiql.integration.graphql, 'query_object.graphql') +
        load_graphql_file(jessiql.integration.graphql, 'object.graphql')
    )


def load_graphql_file(module, filename: str):
    """ Load *.graphql file from a module """
    with open(os.path.join(*module.__path__, filename), 'rt') as f:
        return f.read()
