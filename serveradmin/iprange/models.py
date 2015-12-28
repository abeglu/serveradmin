from copy import copy
from ipaddress import summarize_address_range, IPv4Address

from django.db import models, connection

from serveradmin.common import dbfields
from serveradmin.dataset.base import lookups
from serveradmin.dataset.exceptions import DatasetError
from serveradmin.dataset.querybuilder import QueryBuilder
from serveradmin.dataset import filters
from serveradmin.serverdb.models import Segment

IP_CHOICES = (
    ('ip', 'Private'),
    ('public_ip', 'Public'),
)

class IPRange(models.Model):
    range_id = models.CharField(max_length=20, primary_key=True)
    segment = models.ForeignKey(Segment)
    ip_type = models.CharField(max_length=10, choices=IP_CHOICES)
    min = dbfields.IPv4Field(null=True)
    max = dbfields.IPv4Field(null=True)
    gateway = dbfields.IPv4Field(null=True)
    internal_gateway = dbfields.IPv4Field(null=True)
    min6 = dbfields.IPv6Field(null=True)
    max6 = dbfields.IPv6Field(null=True)
    gateway6 = dbfields.IPv6Field(null=True)
    internal_gateway6 = dbfields.IPv6Field(null=True)
    vlan = models.IntegerField(null=True)
    belongs_to = models.ForeignKey('self', null=True, blank=True,
            related_name='subnet_of')

    def get_network(self):
        networks = tuple(summarize_address_range(self.min, self.max))
        if len(networks) > 1:
            raise ValueError(
                'The min and max addresses does not represent a CIDR '
                'address'
            )

        return networks[0]

    def get_network6(self):
        networks = tuple(summarize_address_range(self.min6, self.max6))
        if len(networks) > 1:
            raise ValueError(
                'The min and max addresses does not represent a CIDR '
                'address'
            )

        return networks[0]

    def get_taken_set(self):
        # Query taken IPs
        if self.min is None or self.max is None:
            return set()

        f_between = filters.Between(self.min, self.max)
        builder = QueryBuilder()
        builder.add_attribute('all_ips')
        builder.add_filter('all_ips', f_between)
        fields = lookups.attr_names['all_ips'].special.attrs
        builder.add_select(*fields)

        # Collect taken IPs in set
        taken_ips = set()
        c = connection.cursor()
        c.execute(builder.build_sql())
        for ip_tuple in c.fetchall():
            for ip in ip_tuple:
                if ip is not None:
                    ip = IPv4Address(ip)
                    if self.min <= ip <= self.max:
                        taken_ips.add(ip)

        return taken_ips

    def get_free_set(self):
        if self.min is None or self.max is None:
            return set()

        free_ips = set()
        taken_ips = self.get_taken_set()
        for ip in self.get_network().hosts():
            if ip not in taken_ips:
                free_ips.add(ip)

        return free_ips

    @property
    def cidr(self):
        try:
            return str(self.get_network())
        except (TypeError, ValueError):
            return None

    @property
    def cidr6(self):
        try:
            return str(self.get_network6())
        except (TypeError, ValueError):
            return None

    class Meta:
        db_table = 'ip_range'

    def __unicode__(self):
        return self.range_id

def _is_taken(ip):
    query_parts = []
    query_parts.append(
        '(SELECT COUNT(*) FROM admin_server WHERE intern_ip = %s)')
    ip_attrs = lookups.attr_names['all_ips'].special.attrs
    for attr in ip_attrs:
        attrib_id = lookups.attr_names[attr].pk
        if attrib_id is None:
            continue
        query = ('(SELECT COUNT(*) '
                 'FROM attrib_values '
                 'WHERE value = %s AND attrib_id = {0})').format(
                    attrib_id)
        query_parts.append(query)

    attrib_id = lookups.attr_names['additional_ips'].pk
    query = 'SELECT {0}'.format(' + '.join(query_parts))
    c = connection.cursor()
    c.execute(query, [int(ip)]*len(query_parts))
    result = c.fetchone()[0]
    c.close()
    return result != 0

def _is_taken6(ipv6):
    attrib_id = lookups.attr_names['primary_ip6'].pk
    query = ('SELECT COUNT(*) FROM attrib_values '
             '        WHERE value = %s AND attrib_id = {0}').format(attrib_id)
    c = connection.cursor()
    c.execute(query, (''.join('{:02x}'.format(x) for x in value.packed), ))
    result = c.fetchone()[0]
    c.close()
    return result != 0

def get_gateways(ip):

    ipranges = IPRange.objects.filter(min__lte=ip, max__gte=ip)
    ipranges = [iprange for iprange in ipranges if iprange is not None]

    if not ipranges:
        raise ValueError('IP is not in known IP ranges')

    def get_range_size(iprange_obj):
        return int(iprange_obj.max) - int(iprange_obj.min)

    iprange_obj = min(ipranges, key=get_range_size)

    def get_netmask(iprange_obj):
        if iprange_obj.ip_type == 'ip':
            return '255.255.0.0'
        else:
            return str(iprange_obj.get_network().netmask)

    def get_gw(iprange_obj, gw_attr):
        if getattr(iprange_obj, gw_attr, None) is not None:
            return [getattr(iprange_obj, gw_attr), get_netmask(iprange_obj)]
        if iprange_obj.belongs_to_id is None:
            return None

        iprange_obj = IPRange.objects.get(range_id=iprange_obj.belongs_to_id)
        return get_gw(iprange_obj, gw_attr)

    return {
        'default_gateway': get_gw(iprange_obj, 'gateway'),
        'internal_gateway': get_gw(iprange_obj, 'internal_gateway')
    }

def get_gateways6(ip):

    ipranges = IPRange.objects.filter(min6__lte=ip, max6__gte=ip)
    ipranges = [iprange for iprange in ipranges if iprange is not None]

    if not ipranges:
        raise ValueError('IP is not in known IP ranges')

    def get_range_size(iprange_obj):
        return int(iprange_obj.max6) - int(iprange_obj.min6)

    iprange_obj = min(ipranges, key=get_range_size)

    def get_prefix(iprange_obj):
        if iprange_obj.ip_type == 'ip':
            # return only /64 or larger nets
            size = iprange_obj.get_network6().prefixlen
            if size > 64:
                return 64
            else:
                return size
        else:
            return iprange_obj.get_network6().prefixlen

    def get_gw(iprange_obj, gw_attr):
        if getattr(iprange_obj, gw_attr, None) is not None:
            return [getattr(iprange_obj, gw_attr), get_prefix(iprange_obj)]
        if iprange_obj.belongs_to_id is None:
            return None

        iprange_obj = IPRange.objects.get(range_id=iprange_obj.belongs_to_id)
        return get_gw(iprange_obj, gw_attr)

    return {
        'default_gateway': get_gw(iprange_obj, 'gateway6'),
        'internal_gateway': get_gw(iprange_obj, 'internal_gateway6')
    }

def _get_network_settings(ip):
    ipranges = IPRange.objects.filter(min__lte=ip, max__gte=ip)
    ipranges = [iprange for iprange in ipranges if iprange is not None]

    if not ipranges:
        raise ValueError('IP is not in known IP ranges')

    def get_range_size(iprange_obj):
        return int(iprange_obj.max) - int(iprange_obj.min)

    # This is the smallest matching IP range.
    # For most of things we will traverse to his parents.
    iprange_obj = min(ipranges, key=get_range_size)

    def calculate_netmask(iprange_obj):
            return iprange_obj.get_network().netmask

    def calculate_prefix(iprange_obj):
            return iprange_obj.get_network().prefixlen

    # Traverse to parent ip_range if given parameter is not specified.
    def nonempty_parent(iprange_obj, param):
        if getattr(iprange_obj, param, None) is not None:
            return getattr(iprange_obj, param)
        if iprange_obj.belongs_to_id is None:
            return None
        iprange_obj = IPRange.objects.get(range_id=iprange_obj.belongs_to_id)
        return nonempty_parent(iprange_obj, param)

    # Traverse to parent ip_range if there is any.
    def highest_parent(iprange_obj):
        if iprange_obj.belongs_to_id:
            iprange_obj = IPRange.objects.get(range_id=iprange_obj.belongs_to_id)
            return highest_parent(iprange_obj)
        else:
            return iprange_obj

    default_gateway = nonempty_parent(iprange_obj, 'gateway')
    internal_gateway = nonempty_parent(iprange_obj, 'internal_gateway')

    return {
        'default_gateway':  str(default_gateway) if default_gateway else None,
        'internal_gateway': str(internal_gateway) if internal_gateway else None,
        'vlan': nonempty_parent(iprange_obj, 'vlan'),
        'ip_type': str(iprange_obj.ip_type),
        'broadcast': str(highest_parent(iprange_obj).max),
        'netmask': calculate_netmask(highest_parent(iprange_obj)),
        'prefix_hi': calculate_prefix(highest_parent(iprange_obj)),
        'prefix_lo': calculate_prefix(iprange_obj),
        'subnet_name': highest_parent(iprange_obj).range_id,
    }

def _get_network_settings6(ip):
    ipranges = IPRange.objects.filter(min6__lte=ip, max6__gte=ip)
    ipranges = [iprange for iprange in ipranges if iprange is not None]

    if not ipranges:
        raise ValueError('IP is not in known IP ranges')

    def get_range_size(iprange_obj):
        return int(iprange_obj.max6) - int(iprange_obj.min6)

    # This is the smallest matching IP range.
    # For most of things we will traverse to his parents.
    iprange_obj = min(ipranges, key=get_range_size)

    def calculate_prefix(iprange_obj):
            return iprange_obj.get_network6().prefixlen

    # Traverse to parent ip_range if given parameter is not specified.
    def nonempty_parent(iprange_obj, param):
        if getattr(iprange_obj, param, None) is not None:
            return getattr(iprange_obj, param)
        if iprange_obj.belongs_to_id is None:
            return None
        iprange_obj = IPRange.objects.get(range_id=iprange_obj.belongs_to_id)
        return nonempty_parent(iprange_obj, param)

    # Traverse to parent ip_range if there is any.
    def highest_parent(iprange_obj):
        if iprange_obj.belongs_to_id:
            iprange_obj = IPRange.objects.get(range_id=iprange_obj.belongs_to_id)
            return highest_parent(iprange_obj)
        else:
            return iprange_obj

    default_gateway6 = nonempty_parent(iprange_obj, 'gateway6')
    internal_gateway6 = nonempty_parent(iprange_obj, 'internal_gateway6')

    return {
        'default_gateway':  str(default_gateway6) if default_gateway6 else None,
        'internal_gateway': str(internal_gateway6) if internal_gateway6 else None,
        'vlan': nonempty_parent(iprange_obj, 'vlan'),
        'ip_type': str(iprange_obj.ip_type),
        'prefix_hi': calculate_prefix(highest_parent(iprange_obj)),
        'prefix_lo': calculate_prefix(iprange_obj)
    }

def _get_iprange_settings(name):
    ipranges = IPRange.objects.filter(range_id=name)
    if not len(ipranges)==1:
        raise ValueError('IP Range not found by name')
    iprange_obj = ipranges[0]

    def calculate_netmask(iprange_obj):
        if iprange_obj.min is None or iprange_obj.min is None:
            return str(None)
        return iprange_obj.get_network6().netmask

    def calculate_netmask6(iprange_obj):
        if iprange_obj.min6 is None or iprange_obj.min6 is None:
            return str(None)
        return iprange_obj.get_network6().prefixlen

    return {
        'default_gateway':  str(iprange_obj.gateway) if iprange_obj.gateway else None,
        'default_gateway6':  str(iprange_obj.gateway6) if iprange_obj.gateway6 else None,
        'internal_gateway': str(iprange_obj.internal_gateway) if iprange_obj.internal_gateway else None,
        'internal_gateway6': str(iprange_obj.internal_gateway6) if iprange_obj.internal_gateway6 else None,
        'vlan': iprange_obj.vlan,
        'ip_type': iprange_obj.ip_type,
        'broadcast': str(iprange_obj.max) if iprange_obj.max else None,
        'netmask': calculate_netmask(iprange_obj),
        'netmask6': calculate_netmask6(iprange_obj),
        'network': str(iprange_obj.min) if iprange_obj.min else None,
        'network6': str(iprange_obj.min6) if iprange_obj.min6 else None
    }
