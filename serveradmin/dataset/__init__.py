from django.db import connection

from adminapi.dataset.base import BaseQuerySet, BaseServerObject
from adminapi.utils import IP
from serveradmin.dataset.base import lookups
from serveradmin.dataset.filters import Optional as _Optional, _prepare_filter

class QuerySet(BaseQuerySet):
    def commit(self):
        print 'I am not implemented yet, but would normally commit changes'
        self._confirm_changes()

    def get_raw_results(self):
        self._get_results()
        return self._results

    def _fetch_results(self):
        # XXX: Dirty hack for the old database structure
        attr_exceptions = {
                'hostname': 'hostname', 
                'intern_ip': 'intern_ip',
                'segment': 'segment',
                'servertype': 'servertype_id'
        }
        i = 0
        sql_left_joins = []
        sql_from = ['admin_server AS adms']
        sql_where = []
        attr_names = lookups.attr_names
        for attr, f in self._filters.iteritems():
            if attr in attr_exceptions:
                attr_field = attr_exceptions[attr]
                if isinstance(f, _Optional):
                    sql_where.append('({0} IS NULL OR {1})'.format(attr_field,
                        f.as_sql_expr(attr, attr_field)))
                else:
                    sql_where.append(f.as_sql_expr(attr, attr_field))
            else:
                attr_field = 'av{0}.value'.format(i)
                if isinstance(f, _Optional):
                    join = ('LEFT JOIN attrib_values AS av{0} '
                            'ON av{0}.server_id = adms.server_id AND '
                            'av{0}.attrib_id = {1} AND {2}').format(i,
                                attr_names[attr].pk,
                                f.as_sql_expr(attr, attr_field))
                    sql_left_joins.append(join)
                else:
                    sql_from.append('attrib_values AS av{0}'.format(i))
                    sql_where += [
                        'av{0}.server_id = adms.server_id'.format(i),
                        'av{0}.attrib_id = {1}'.format(i, attr_names[attr].pk),
                        f.as_sql_expr(attr, attr_field)
                    ]
        
                i += 1
        
        sql_stmt = '\n'.join([
                'SELECT adms.server_id, adms.hostname, adms.intern_ip, '
                'adms.segment, adms.servertype_id',
                'FROM',
                ', '.join(sql_from),
                '\n'.join(sql_left_joins),
                'WHERE' if sql_where else '',
                '\n AND '.join(sql_where),
                'GROUP BY adms.server_id'
        ])

        c = connection.cursor()
        c.execute(sql_stmt)
        server_data = {}
        servertype_lookup = dict((k, v.name) for k, v in
                lookups.stype_ids.iteritems())
        restrict = self._restrict
        for server_id, hostname, intern_ip, segment, stype in c.fetchall():
            if not restrict:
                attrs = {
                    u'hostname': hostname,
                    u'intern_ip': IP(intern_ip),
                    u'segment': segment,
                    u'servertype': servertype_lookup[stype]
                }
            else:
                attrs = {}
                if 'hostname' in restrict:
                    attrs['hostname'] = hostname
                if 'intern_ip' in restrict:
                    attrs['intern_ip'] = IP(intern_ip)
                if 'segment' in restrict:
                    attrs['segment'] = segment
                if 'servertype' in restrict:
                    attrs['servertype'] = servertype_lookup[stype]

            server_data[server_id] = ServerObject(attrs, server_id, self)
        
        # Return early if there are no servers (= empty dict)
        if not server_data:
            return server_data
        
        # Remove attributes from adm_server from the restrict set
        if restrict:
            restrict = restrict - set(attr_exceptions.iterkeys())
            # if restrict is empty now, there are no attributes to fetch
            # from the attrib_values table, but just attributes from
            # admin_server table. We can return early
            if not restrict:
                return server_data

        server_ids = ', '.join(map(str, server_data.iterkeys()))
        sql_stmt = ('SELECT server_id, attrib_id, value FROM attrib_values '
                    'WHERE server_id IN({0})').format(server_ids)
        
        if restrict:
            restrict_ids = ', '.join(str(lookups.attr_names[attr_name].pk)
                    for attr_name in restrict)
            sql_stmt += ' AND attrib_id IN({0})'.format(restrict_ids)
        
        c.execute(sql_stmt)
        attr_ids = lookups.attr_ids
        for server_id, attr_id, value in c.fetchall():
            attr = attr_ids[attr_id]
            attr_type = attr.type
            if attr_type == 'integer':
                value = int(value)
            elif attr_type == 'boolean':
                value = value == '1'
            elif attr_type == 'ip':
                value = IP(value)
            
            # Using dict-methods to bypass ServerObject's special properties
            if attr.multi:
                values = dict.setdefault(server_data[server_id], attr.name, set())
                values.add(value)
            else:
                dict.__setitem__(server_data[server_id], attr.name, value)
        
        return server_data

class ServerObject(BaseServerObject):
    def commit(self):
        print 'I am not implemented yet, but would normally commit changes'
        self._confirm_changes()

def query(**kwargs):
    filters = dict((k, _prepare_filter(v)) for k, v in kwargs.iteritems())
    return QuerySet(filters)
