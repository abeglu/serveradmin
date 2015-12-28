import re
import operator

from ipaddress import ip_network

from django.db import connection, DatabaseError
try:
    from oursql import OperationalError
except ImportError:
    OperationalError = DatabaseError

from serveradmin.dataset.base import lookups
from serveradmin.dataset.exceptions import DatasetError
from serveradmin.dataset.typecast import typecast
from serveradmin.dataset.sqlhelpers import value_to_sql, raw_sql_escape

class BaseFilter(object):
    def __and__(self, other):
        return And(self, other)

    def __or__(self, other):
        return Or(self, other)

    def __ne__(self, other):
        return not self.__eq__(other)

class NoArgFilter(BaseFilter):
    def __repr__(self):
        return u'{0}()'.format(self.__class__.__name__)

    def __eq__(self, other):
        return isinstance(other, self.__class__)

    def __hash__(self):
        return hash(self.__class__.__name__)

    def as_sql_expr(self, builder, attr_obj, field):
        return self.filt.as_sql_expr(builder, attr_obj, field)

    def matches(self, server_obj, attr_name):
        return self.filt.matches(server_obj, attr_name)

    def as_code(self):
        return u'filters.{0}()'.format(self.__class__.__name__)

    def typecast(self, attr_name):
        # We don't have values to typecast
        pass

    @classmethod
    def from_obj(cls, obj):
        return cls()

# We need this class to group optional filters.
class OptionalFilter(BaseFilter):
    pass

class ExactMatch(BaseFilter):
    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return u'ExactMatch({0!r})'.format(self.value)

    def __eq__(self, other):
        if isinstance(other, ExactMatch):
            return self.value == other.value
        return False

    def __hash__(self):
        return hash(u'ExactMatch') ^ hash(self.value)

    def as_sql_expr(self, builder, attr_obj, field):
        if attr_obj.type == 'boolean' and not self.value:
            return u"({0} = '0' OR {0} IS NULL)".format(field)
        return u'{0} = {1}'.format(field, value_to_sql(attr_obj, self.value))

    def matches(self, server_obj, attr_name):
        return server_obj[attr_name] == self.value

    def as_code(self):
        return repr(self.value)

    def typecast(self, attr_name):
        self.value = typecast(attr_name, self.value, force_single=True)

    @classmethod
    def from_obj(cls, obj):
        if u'value' in obj:
            return cls(obj[u'value'])

        raise ValueError('Invalid object for ExactMatch')

class Regexp(BaseFilter):
    def __init__(self, regexp):
        try:
            self._regexp_obj = re.compile(regexp)
        except re.error as e:
            raise ValueError(u'Invalid regexp: ' + unicode(e))

        self.regexp = regexp

    def __repr__(self):
        return u'Regexp({0!r})'.format(self.regexp)

    def __eq__(self, other):
        if isinstance(other, Regexp):
            return self.regexp == other.regexp
        return False

    def __hash__(self):
        return hash(u'Regexp') ^ hash(self.regexp)

    def as_sql_expr(self, builder, attr_obj, field):
        # XXX Dirty hack for servertype regexp checking
        if attr_obj.name == u'servertype':
            stype_ids = []
            for stype in lookups.stype_ids.itervalues():
                if self._regexp_obj.search(stype.name):
                    stype_ids.append(unicode(stype.pk))
            if stype_ids:
                return u'{0} IN ({1})'.format(field, ', '.join(stype_ids))
            else:
                return u'0=1'
        elif attr_obj.type == u'ip':
            sql_regexp = raw_sql_escape(self.regexp)
            return u'INET_NTOA({0}) REGEXP {1}'.format(field, sql_regexp)
        else:
            sql_regexp = raw_sql_escape(self.regexp)
            return u'{0} REGEXP {1}'.format(field, sql_regexp)

    def matches(self, server_obj, attr_name):
        value = str(server_obj[attr_name])
        return bool(self._regexp_obj.search(value))

    def as_code(self):
        return u'filters.' + repr(self)

    def typecast(self, attr_name):
        # Regexp value is always string, no need to typecast
        pass

    @classmethod
    def from_obj(cls, obj):
        if u'regexp' in obj and isinstance(obj[u'regexp'], basestring):
            return cls(obj[u'regexp'])
        raise ValueError(u'Invalid object for Regexp')

class Comparison(BaseFilter):
    def __init__(self, comparator, value):
        if comparator not in (u'<', u'>', u'<=', u'>='):
            raise ValueError(u'Invalid comparison operator: ' + comparator)
        self.comparator = comparator
        self.value = value

    def __repr__(self):
        return u'Comparison({0!r}, {1!r})'.format(self.comparator, self.value)

    def __eq__(self, other):
        if isinstance(other, Comparison):
            return (self.comparator == other.comparator and
                    self.value == other.value)
        return False

    def __hash__(self):
        return hash(u'Comparison') ^ hash(self.comparator) ^ hash(self.value)

    def as_sql_expr(self, builder, attr_obj, field):
        return u'{0} {1} {2}'.format(
                field,
                self.comparator,
                value_to_sql(attr_obj, self.value)
            )

    def matches(self, server_obj, attr_name):

        if self.comparator == '<':
            op = operator.lt
        elif self.comparator == '>':
            op = operator.gt
        elif operator.le == '<=':
            op = operator.le
        elif operator.gt == '>=':
            op = operator.gt
        else:
            raise ValueError('Operator doesn\'t exists')

        return op(server_obj[attr_name], self.value)

    def as_code(self):
        return u'filters.' + repr(self)

    def typecast(self, attr_name):
        self.value = typecast(attr_name, self.value, force_single=True)

    @classmethod
    def from_obj(cls, obj):
        if u'comparator' in obj and u'value' in obj:
            return cls(obj[u'comparator'], obj[u'value'])
        raise ValueError(u'Invalid object for Comparison')

class Any(BaseFilter):
    def __init__(self, *values):
        self.values = set(values)

    def __repr__(self):
        return u'Any({0})'.format(', '.join(repr(val) for val in self.values))

    def __eq__(self, other):
        if isinstance(other, Any):
            return self.values == other.values
        return False

    def __hash__(self):
        h = hash(u'Any')
        for val in self.values:
            h ^= hash(val)
        return h

    def as_sql_expr(self, builder, attr_obj, field):
        if not self.values:
            return u'0 = 1'

        prepared_values = u', '.join(
            value_to_sql(attr_obj, value) for value in self.values
        )

        return u'{0} IN ({1})'.format(field, prepared_values)

    def matches(self, server_obj, attr_name):
        return server_obj[attr_name] in self.values

    def as_code(self):
        return u'filters.' + repr(self)

    def typecast(self, attr_name):
        self.values = set(typecast(attr_name, x, force_single=True)
                          for x in self.values)

    @classmethod
    def from_obj(cls, obj):
        if u'values' in obj and isinstance(obj[u'values'], list):
            return cls(*obj[u'values'])
        raise ValueError(u'Invalid object for Any')

class _AndOr(BaseFilter):
    def __init__(self, *filters):
        self.filters = map(_prepare_filter, filters)

    def __repr__(self):
        args = u', '.join(repr(filt) for filt in self.filters)
        return u'{0}({1})'.format(self.name.capitalize(), args)

    def __eq__(self, other):

        if isinstance(other, self.__class__):
            return self.filters == other.filters

        return False

    def __hash__(self):

        result = hash(self.name)
        for value in self.filters:
            result ^= hash(value)

        return result

    def as_sql_expr(self, builder, attr_obj, field):

        joiner = u' {0} '.format(self.name.upper())

        return u'({0})'.format(joiner.join([
            filter.as_sql_expr(builder, attr_obj, field)
            for filter in self.filters
        ]))

    def as_code(self):

        args = u', '.join(filt.as_code() for filt in self.filters)

        return u'filters.{0}({1})'.format(self.name.capitalize(), args)

    def typecast(self, attr_name):
        for filt in self.filters:
            filt.typecast(attr_name)

    @classmethod
    def from_obj(cls, obj):

        if u'filters' in obj and isinstance(obj[u'filters'], list):
            if not obj['filters']:
                raise ValueError('Empty filters for And/Or')

            return cls(*[filter_from_obj(filter) for filter in obj[u'filters']])

        raise ValueError(u'Invalid object for {0}'.format(
            cls.__name__.capitalize())
        )

class And(_AndOr):
    name = u'and'

    def matches(self, server_obj, attr_name):

        for filter in self.filters:
            if not filter.matches(server_obj, attr_name):
                return False

        return True

class Or(_AndOr):
    name = u'or'

    def matches(self, server_obj, attr_name):

        for filter in self.filters:
            if filter.matches(server_obj, attr_name):
                return True

        return False

class Between(BaseFilter):

    def __init__(self, a, b):
        self.a = a
        self.b = b

    def __repr__(self):
        return u'Between({0!r}, {1!r})'.format(self.a, self.b)

    def __eq__(self, other):
        if isinstance(other, Between):
            return self.a == other.a and self.b == other.b
        return False

    def __hash__(self):
        return hash(u'Between') ^ hash(self.a) ^ hash(self.b)

    def as_sql_expr(self, builder, attr_obj, field):

        a_prepared = value_to_sql(attr_obj, self.a)
        b_prepared = value_to_sql(attr_obj, self.b)

        return u'{0} BETWEEN {1} AND {2}'.format(field, a_prepared, b_prepared)

    def matches(self, server_obj, attr_name):
        return self.a <= server_obj[attr_name] <= self.b

    def as_code(self):
        return u'filters.' + repr(self)

    def typecast(self, attr_name):
        self.a = typecast(attr_name, self.a, force_single=True)
        self.b = typecast(attr_name, self.b, force_single=True)

    @classmethod
    def from_obj(cls, obj):

        if u'a' in obj and u'b' in obj:
            return cls(obj[u'a'], obj[u'b'])

        raise ValueError(u'Invalid object for Between')

class Not(BaseFilter):
    def __init__(self, filter):
        self.filter = _prepare_filter(filter)

    def __repr__(self):
        return u'Not({0!})'.format(self.filter)

    def __eq__(self, other):

        if isinstance(other, Not):
            return self.filter == other.filter

        return False

    def __hash__(self):
        return hash(u'Not') ^ hash(self.filter)

    def as_sql_expr(self, builder, attr_obj, field):

        if attr_obj.multi:
            uid = builder.get_uid()

            # Special case for empty filter, simple negation doesn't work
            # here. It would just return all empty values, instead of values
            # which are NOT empty.
            if isinstance(self.filter, Empty):
                return (
                    'EXISTS (SELECT 1 FROM attrib_values AS nav{0} '
                            'WHERE nav{0}.server_id = adms.server_id AND '
                                    'nav{0}.attrib_id = {1})'
                ).format(uid, attr_obj.attrib_id)

            cond = self.filter.as_sql_expr(
                    builder,
                    attr_obj,
                    'nav{0}.value'.format(uid),
                )

            subquery = (
                    'SELECT id FROM attrib_values AS nav{0} '
                        'WHERE {1} AND '
                            'nav{0}.server_id = adms.server_id AND '
                            'nav{0}.attrib_id = {2}'
                ).format(uid, cond, attr_obj.attrib_id)

            return 'NOT EXISTS ({0})'.format(subquery)
        else:
            if isinstance(self.filter, ExactMatch):
                return u'{0} != {1}'.format(
                    field,
                    value_to_sql(attr_obj, self.filter.value),
                )
            else:
                return u'NOT {0}'.format(
                    self.filter.as_sql_expr(builder, attr_obj, field),
                )

    def matches(self, server_obj, attr_name):
        return not self.filter.matches(server_obj, attr_name)

    def as_code(self):
        return u'filters.Not({0})'.format(self.filter.as_code())

    def typecast(self, attr_name):
       self.filter.typecast(attr_name)

    @classmethod
    def from_obj(cls, obj):

        if u'filter' in obj:
            return cls(filter_from_obj(obj[u'filter']))

        raise ValueError(u'Invalid object for Not')

class Startswith(BaseFilter):
    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return u'Startswith({0!})'.format(self.value)

    def __eq__(self, other):
        if isinstance(other, Startswith):
            return self.value == other.value

    def __hash__(self):
        return hash(u'Startswith') ^ hash(self.value)

    def as_sql_expr(self, builder, attr_obj, field):

        # XXX Dirty hack for servertype checking
        if attr_obj.name == u'servertype':
            stype_ids = []

            for stype in lookups.stype_ids.itervalues():
                if stype.name.startswith(self.value):
                    stype_ids.append(unicode(stype.pk))
            if stype_ids:
                return u'{0} IN ({1})'.format(field, ', '.join(stype_ids))
            else:
                return u'0 = 1'

        if attr_obj.type == u'ip':
            value = raw_sql_escape(str(self.value) + '%%')

            return u'INET_NTOA({0}) LIKE {1}'.format(field, value)

        if attr_obj.type == 'string':
            value = self.value.replace('_', '\\_').replace(u'%', u'\\%%')
            value = raw_sql_escape(value + u'%%')

            return u'{0} LIKE {1}'.format(field, value)

        if attr_obj.type == 'integer':
            try:
                return u"{0} LIKE '{1}%'".format(int(self.value))
            except ValueError:
                return u'0 = 1'

        return u'0 = 1'

    def matches(self, server_obj, attr_name):
        return unicode(server_obj[attr_name]).startswith(self.value)

    def as_code(self):
        return u'filters.Startswith({0!r})'.format(self.value)

    def typecast(self, attr_name):
        self.value = unicode(self.value)

    @classmethod
    def from_obj(cls, obj):
        if u'value' in obj and isinstance(obj[u'value'], basestring):
            return cls(obj[u'value'])
        raise ValueError(u'Invalid object for Startswith')

class InsideNetwork(BaseFilter):
    def __init__(self, *networks):
        self.networks = [ip_network(n) for n in networks]

    def __repr__(self):
        return u'InsideNetwork({0})'.format(
            ', '.join(repr(n) for n in self.networks)
        )

    def __eq__(self, other):

        if isinstance(other, InsideNetwork):
            return all(
                n1 == n2 for n1, n2 in zip(self.networks, other.networks)
            )

        return False

    def __hash__(self):

        result = hash('InsideNetwork')
        for network in self.networks:
            result ^= hash(network)

        return result

    def as_sql_expr(self, builder, attr_obj, field):

        betweens = ['{0} BETWEEN {1} AND {2}'.format(
            field,
            int(net.network_address),
            int(net.broadcast_address),
        ) for net in self.networks]

        return u'({0})'.format(u' OR '.join(betweens))

    def matches(self, server_obj, attr_name):

        return any(
            net.min_ip <= server_obj[attr_name] <= net.max_ip
            for net in self.networks
        )

    def as_code(self):
        return u'filters.' + repr(self)

    def typecast(self, attr_name):
        # Typecast was already done in __init__
        pass

    @classmethod
    def from_obj(cls, obj):

        if u'networks' in obj and isinstance(obj['networks'], (tuple, list)):
            return cls(*obj[u'networks'])

        raise ValueError(u'Invalid object for InsideNetwork')

class PrivateIP(NoArgFilter):

    blocks = (
        ip_network('10.0.0.0/8'),
        ip_network('172.16.0.0/12'),
        ip_network('192.168.0.0/16'),
    )

    def __init__(self):
        self.filt = InsideNetwork(*PrivateIP.blocks)

class PublicIP(NoArgFilter):

    def __init__(self):
        self.filt = Not(InsideNetwork(*PrivateIP.blocks))

class Optional(OptionalFilter):
    def __init__(self, filter):
        self.filter = _prepare_filter(filter)

    def __repr__(self):
        return u'Optional({0!r})'.format(self.filter)

    def __eq__(self, other):
        if isinstance(other, Optional):
            return self.filter == other.filter
        return False

    def __hash__(self):
        return hash(u'Optional') ^ hash(self.filter)

    def as_sql_expr(self, builder, attr_obj, field):
        return u'({0} IS NULL OR {1})'.format(
            field,
            self.filter.as_sql_expr(builder, attr_obj, field),
        )

    def matches(self, server_obj, attr_name):

        value = server_obj.get(attr_name)
        if value is None:
            return True

        return self.filter.matches(server_obj, attr_name)

    def as_code(self):
        return u'filters.Optional({0})'.format(self.filter.as_code())

    def typecast(self, attr_name):
        self.filter.typecast(attr_name)

    @classmethod
    def from_obj(cls, obj):

        if u'filter' in obj:
            return cls(filter_from_obj(obj[u'filter']))

        raise ValueError(u'Invalid object for Optional')

class Empty(OptionalFilter):
    def __repr__(self):
        return u'Empty()'

    def __eq__(self, other):
        return isinstance(other, Empty)

    def __hash__(self):
        return hash('Empty')

    def as_sql_expr(self, builder, attr_obj, field):
        return u'{0} IS NULL'.format(field)

    def matches(self, server_obj, attr_name):
        return attr_name not in server_obj or len(server_obj[attr_name]) == 0

    def as_code(self):
        return u'filters.Empty()'

    def typecast(self, attr_name):
        pass

    @classmethod
    def from_obj(cls, obj):
        return cls()

def _prepare_filter(filter):
    return filter if isinstance(filter, BaseFilter) else ExactMatch(filter)

def filter_from_obj(obj):

    if not (
            isinstance(obj, dict)
        and
            u'name' in obj
        and
            isinstance(obj[u'name'], basestring)
    ):
        raise ValueError(u'Invalid filter object')

    try:
        return filter_classes[obj[u'name']].from_obj(obj)
    except KeyError:
        raise ValueError(u'No such filter: {0}'.format(obj[u'name']))

filter_classes = {
    'exactmatch': ExactMatch,
    'regexp': Regexp,
    'comparison': Comparison,
    'any': Any,
    'any': Any,
    'and': And,
    'or': Or,
    'between': Between,
    'not': Not,
    'startswith': Startswith,
    'insidenetwork': InsideNetwork,
    'privateip': PrivateIP,
    'publicip': PublicIP,
    'optional': Optional,
    'empty': Empty,
}
