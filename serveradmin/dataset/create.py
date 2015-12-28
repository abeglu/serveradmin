import json
from ipaddress import (
    ip_address,
    IPv4Address,
    IPv6Address,
    IPv4Network,
    IPv6Network,
)

from django.db import connection

from serveradmin.serverdb.models import ChangeCommit, ChangeAdd
from serveradmin.dataset.base import lookups
from serveradmin.dataset.validation import handle_violations, check_attribute_type
from serveradmin.dataset.typecast import typecast
from serveradmin.dataset.exceptions import CommitError
from serveradmin.dataset.sqlhelpers import prepare_value
from adminapi.utils.json import json_encode_extra

def create_server(
        attributes,
        skip_validation,
        fill_defaults,
        fill_defaults_all,
        user=None,
        app=None,
    ):

    # Import here to break cyclic imports.
    from serveradmin.iprange.models import IPRange

    if u'hostname' not in attributes:
        raise CommitError(u'Hostname is required')
    if u'servertype' not in attributes:
        raise CommitError(u'Servertype is required')
    if u'project' not in attributes:
        raise CommitError(u'Project is required')
    if u'intern_ip' not in attributes:
        raise CommitError(u'Internal IP (intern_ip) is required')

    for attr in (u'hostname', u'servertype', u'project', u'intern_ip'):
        check_attribute_type(attr, attributes[attr])

    try:
        stype = lookups.stype_names[attributes[u'servertype']]
    except KeyError:
        raise CommitError(u'Unknown servertype: ' + attributes[u'servertype'])

    hostname = attributes[u'hostname']
    if isinstance(attributes['intern_ip'], (
        IPv4Address,
        IPv6Address,
        IPv4Network,
        IPv6Network,
    )):
        intern_ip = attributes['intern_ip']
    else:
        intern_ip = ip_address(attributes[u'intern_ip'])
    servertype_id = stype.pk
    segment = attributes.get(u'segment')

    if segment:
        check_attribute_type(u'segment', segment)
    else:
        try:
            segment = IPRange.objects.filter(
                    min__lte=intern_ip,
                    max__gte=intern_ip,
                )[0].segment
        except IndexError:
            raise CommitError('Could not determine segment')

    project_id = attributes.get(u'project')

    real_attributes = attributes.copy()
    for key in (
            u'hostname',
            u'intern_ip',
            u'comment',
            u'servertype',
            u'segment',
            u'project',
        ):
        if key in real_attributes:
            del real_attributes[key]

    violations_regexp = []
    violations_required = []
    for attr in stype.attributes:
        stype_attr = lookups.stype_attrs[(stype.name, attr.name)]
        attr_obj = lookups.attr_names[attr.name]

        # Handle not existing attributes (fill defaults, validate require)
        if attr.name not in real_attributes:
            if attr_obj.multi:
                if stype_attr.default in ('', None):
                    real_attributes[attr.name] = []
                else:
                    real_attributes[attr.name] = _type_cast_default(attr_obj,
                            stype_attr.default)
            elif stype_attr.required:
                if fill_defaults and stype_attr.default not in ('', None):
                    real_attributes[attr.name] = _type_cast_default(
                            attr_obj,
                            stype_attr.default,
                        )
                else:
                    violations_required.append(attr.name)
                    continue
            else:
                if fill_defaults_all and stype_attr.default not in ('', None):
                    real_attributes[attr.name] = _type_cast_default(
                            attr_obj,
                            stype_attr.default,
                        )
                else:
                    continue

        value = real_attributes[attr.name]
        check_attribute_type(attr.name, value)

        # Validate regular expression
        regexp = stype_attr.regexp
        if attr_obj.multi:
            if attr_obj.type == 'string' and regexp:
                for val in value:
                    if not regexp.match(unicode(val)):
                        violations_regexp.append(attr.name)
        else:
            if attr_obj.type == 'string' and regexp and not regexp.match(value):
                violations_regexp.append(attr.name)

    # Check for attributes that are not defined on this servertype
    violations_attribs = []
    attribute_set = set([attr.name for attr in stype.attributes])
    for attr in real_attributes:
        if attr not in attribute_set:
            violations_attribs.append(attr)

    handle_violations(
            skip_validation,
            violations_regexp,
            violations_required,
            violations_attribs,
        )

    with connection.cursor() as cursor:
        cursor.execute(u"SELECT GET_LOCK('serverobject_commit', 10)")
        if not cursor.fetchone()[0]:
            raise CommitError(u'Could not get lock')
        try:
            server_id = _insert_server(
                    hostname,
                    intern_ip,
                    segment,
                    servertype_id,
                    project_id,
                    real_attributes,
                )
        except:
            raise
        else:
            created_server = real_attributes.copy()
            created_server['hostname'] = hostname
            created_server['intern_ip'] = intern_ip
            created_server['segment'] = str(segment)

            commit = ChangeCommit.objects.create(app=app, user=user)
            attributes_json = json.dumps(created_server, default=json_encode_extra)
            ChangeAdd.objects.create(
                    commit=commit,
                    hostname=created_server['hostname'],
                    attributes_json=attributes_json,
                )
        finally:
            cursor.execute(u'COMMIT')
            cursor.execute(u"SELECT RELEASE_LOCK('serverobject_commit')")

    return server_id

def _insert_server(hostname, intern_ip, segment, servertype_id, project_id, attributes):

    with connection.cursor() as cursor:
        cursor.execute(
                u'SELECT COUNT(*) FROM admin_server WHERE hostname = %s',
                (hostname, )
            )
        if cursor.fetchone()[0] != 0:
            raise CommitError(u'Server with that hostname already exists')

        # Get segment
        cursor.execute(
                u'SELECT segment_id FROM ip_range '
                u'WHERE %s BETWEEN `min` AND `max` LIMIT 1',
                (int(intern_ip), )
            )
        result = cursor.fetchone()
        segment_id = result[0] if result else segment

        # Insert into admin_server table
        cursor.execute(
                u'INSERT INTO admin_server ('
                    u'hostname, '
                    u'intern_ip, '
                    u'servertype_id, '
                    u'segment, '
                    u'project_id'
                u') VALUES (%s, %s, %s, %s, %s)',
                (
                    hostname,
                    int(intern_ip),
                    servertype_id,
                    segment_id,
                    project_id,
                )
            )
        server_id = cursor.lastrowid

        # Insert additional attributes
        attr_query = (
                u'INSERT INTO attrib_values (server_id, attrib_id, value) '
                u'VALUES (%s, %s, %s)'
            )

        for attr_name, value in attributes.iteritems():
            attr_obj = lookups.attr_names[attr_name]
            if attr_obj.multi:
                for single_value in value:
                    single_value = prepare_value(attr_obj, single_value)
                    cursor.execute(attr_query, (server_id, attr_obj.pk, single_value))
            else:
                value = prepare_value(attr_obj, value)
                cursor.execute(attr_query, (server_id, attr_obj.pk, value))

    return server_id

def _type_cast_default(attr_obj, value):
    if attr_obj.multi:
        return [
                typecast(attr_obj.name, val, force_single=True)
                for val in value.split(',')
            ]
    else:
        return typecast(attr_obj.name, value, force_single=True)
