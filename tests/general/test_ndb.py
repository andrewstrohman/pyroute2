import uuid
import threading
from utils import grep
from utils import require_user
from utils import skip_if_not_supported
from pyroute2 import netns
from pyroute2 import NDB
from pyroute2 import NetNS
from pyroute2 import IPRoute
from pyroute2 import NetlinkError
from pyroute2 import RemoteIPRoute
from pyroute2.common import uifname
from pyroute2.common import basestring
from pyroute2.ndb import report
from pyroute2.ndb.main import Report
from pyroute2.netlink.rtnl.ifinfmsg import ifinfmsg
from pyroute2.netlink.rtnl.ifaddrmsg import ifaddrmsg
from pyroute2.netlink.rtnl.rtmsg import rtmsg


_locks = {}


class TestMisc(object):

    @skip_if_not_supported
    def test_multiple_sources(self):

        # NB: no 'localhost' record -- important
        #
        sources = {'localhost0': IPRoute(),
                   'localhost1': RemoteIPRoute(),  # local mitogen source
                   'localhost2': RemoteIPRoute()}  # one more

        # check all the views
        #
        with NDB(sources=sources) as ndb:
            assert len(ndb.interfaces.csv())
            assert len(ndb.neighbours.csv())
            assert len(ndb.addresses.csv())
            assert len(ndb.routes.csv())

        for source in sources:
            assert sources[source].closed


class TestBase(object):

    db_provider = 'sqlite3'
    db_spec = ':memory:'
    nl_class = IPRoute
    nl_kwarg = {}
    ssh = ''

    def link_register(self, ifnme):
        global _locks

        def handler(target, event):
            if event.get_attr('IFLA_IFNAME') == ifnme:
                _locks[ifnme].set()

        _locks[ifnme] = threading.Event()
        self.ndb.register_handler(ifinfmsg, handler)

    def addr_register(self, addr):
        global _locks

        def handler(target, event):
            if event.get_attr('IFA_ADDRESS') == addr:
                _locks[addr].set()

        _locks[addr] = threading.Event()
        self.ndb.register_handler(ifaddrmsg, handler)

    def route_register(self, addr):
        global _locks

        def handler(target, event):
            if event.get_attr('RTA_DST') == addr:
                _locks[addr].set()

        _locks[addr] = threading.Event()
        self.ndb.register_handler(rtmsg, handler)

    def route_wait(self, addr):
        global _locks
        _locks[addr].wait(3)

    def addr_wait(self, addr):
        global _locks
        _locks[addr].wait(3)

    def link_wait(self, ifname):
        global _locks
        _locks[ifname].wait(3)
        with self.nl_class(**self.nl_kwarg) as ipr:
            return ipr.link_lookup(ifname=ifname)[0]

    def create_interfaces(self):
        # dummy interface
        if_dummy = uifname()
        if_vlan_stag = uifname()
        if_vlan_ctag = uifname()
        if_bridge = uifname()
        if_port = uifname()
        ret = []
        for ifname in (if_dummy,
                       if_vlan_stag,
                       if_vlan_ctag,
                       if_port,
                       if_bridge):
            self.link_register(ifname)

        with self.nl_class(**self.nl_kwarg) as ipr:

            ipr.link('add',
                     ifname=if_dummy,
                     kind='dummy')
            ret.append(self.link_wait(if_dummy))

            ipr.link('add',
                     ifname=if_vlan_stag,
                     link=ret[-1],
                     vlan_id=101,
                     vlan_protocol=0x88a8,
                     kind='vlan')
            ret.append(self.link_wait(if_vlan_stag))

            ipr.link('add',
                     ifname=if_vlan_ctag,
                     link=ret[-1],
                     vlan_id=1001,
                     vlan_protocol=0x8100,
                     kind='vlan')
            ret.append(self.link_wait(if_vlan_ctag))

            ipr.link('add',
                     ifname=if_port,
                     kind='dummy')
            ret.append(self.link_wait(if_port))

            ipr.link('add',
                     ifname=if_bridge,
                     kind='bridge')
            ret.append(self.link_wait(if_bridge))
            ipr.link('set', index=ret[-2], master=ret[-1])
            return ret

    def setup(self):
        require_user('root')
        self.if_simple = None
        self.ndb = NDB(db_provider=self.db_provider,
                       db_spec=self.db_spec,
                       sources=self.nl_class(**self.nl_kwarg))
        self.interfaces = self.create_interfaces()

    def teardown(self):
        with self.nl_class(**self.nl_kwarg) as ipr:
            for link in reversed(self.interfaces):
                ipr.link('del', index=link)
        self.ndb.close()

    def fetch(self, request, values=[]):
        with self.ndb.schema.db_lock:
            return (self
                    .ndb
                    .schema
                    .execute(request, values)
                    .fetchall())


class TestCreate(object):

    db_provider = 'sqlite3'
    db_spec = ':memory:'
    nl_class = IPRoute
    nl_kwarg = {}
    ssh = ''

    def ifname(self):
        ret = uifname()
        self.interfaces.append(ret)
        return ret

    def setup(self):
        require_user('root')
        self.interfaces = []
        self.ndb = NDB(db_provider=self.db_provider,
                       db_spec=self.db_spec,
                       sources=self.nl_class(**self.nl_kwarg))

    def teardown(self):
        with self.nl_class(**self.nl_kwarg) as ipr:
            for link in reversed(self.interfaces):
                ipr.link('del', index=ipr.link_lookup(ifname=link)[0])
        self.ndb.close()

    def test_context_manager(self):

        ifname = uifname()
        address = '00:11:22:36:47:58'
        ifobj = (self
                 .ndb
                 .interfaces
                 .add(ifname=ifname, kind='dummy'))

        with ifobj:
            pass

        assert grep('%s ip link show' % self.ssh, pattern=ifname)

        with ifobj:
            ifobj['state'] = 'up'
            ifobj['address'] = address

        assert grep('%s ip link show' % self.ssh, pattern=address)
        assert self.ndb.interfaces[ifname]['state'] == 'up'

        with ifobj:
            ifobj.remove()

    def test_fail(self):

        ifname = uifname()
        kind = uifname()

        ifobj = (self
                 .ndb
                 .interfaces
                 .add(ifname=ifname, kind=kind))

        save = dict(ifobj)

        try:
            ifobj.commit()
        except NetlinkError as e:
            assert e.code == 95  # Operation not supported

        assert save == dict(ifobj)
        assert ifobj.scope == 'invalid'

    def test_dummy(self):

        ifname = self.ifname()
        (self
         .ndb
         .interfaces
         .add(ifname=ifname, kind='dummy', address='00:11:22:33:44:55')
         .commit())

        assert grep('%s ip link show' % self.ssh, pattern=ifname)
        assert self.ndb.interfaces[ifname]['address'] == '00:11:22:33:44:55'

    def test_basic_address(self):

        ifname = self.ifname()
        i = (self
             .ndb
             .interfaces
             .add(ifname=ifname, kind='dummy', state='up'))
        i.commit()

        a = (self
             .ndb
             .addresses
             .add(index=i['index'],
                  address='192.168.153.5',
                  prefixlen=24))
        a.commit()
        assert grep('%s ip link show' % self.ssh,
                    pattern=ifname)
        assert grep('%s ip addr show dev %s' % (self.ssh, ifname),
                    pattern='192.168.153.5')

    def test_basic_route(self):

        ifname = self.ifname()
        i = (self
             .ndb
             .interfaces
             .add(ifname=ifname, kind='dummy', state='up'))
        i.commit()

        a = (self
             .ndb
             .addresses
             .add(index=i['index'],
                  address='192.168.154.5',
                  prefixlen=24))
        a.commit()

        r = (self
             .ndb
             .routes
             .add(dst_len=24,
                  dst='192.168.155.0',
                  gateway='192.168.154.10'))
        r.commit()
        assert grep('%s ip link show' % self.ssh,
                    pattern=ifname)
        assert grep('%s ip addr show dev %s' % (self.ssh, ifname),
                    pattern='192.168.154.5')
        assert grep('%s ip route show' % self.ssh,
                    pattern='192.168.155.0/24.*%s' % ifname)


class TestRollback(TestBase):

    def setup(self):
        require_user('root')
        self.ndb = NDB(db_provider=self.db_provider,
                       db_spec=self.db_spec,
                       rtnl_log=True,
                       sources=self.nl_class(**self.nl_kwarg))

    def test_simple_deps(self):

        # register NDB handler to wait for the interface
        self.if_simple = uifname()
        self.link_register(self.if_simple)
        self.addr_register('172.16.172.16')
        self.route_register('172.16.127.0')

        with self.nl_class(**self.nl_kwarg) as ipr:
            self.interfaces = []
            #
            # simple dummy interface with one address and
            # one dependent route
            #
            ipr.link('add',
                     ifname=self.if_simple,
                     kind='dummy')
            self.interfaces.append(self.link_wait(self.if_simple))
            ipr.link('set',
                     index=self.interfaces[-1],
                     state='up')
            ipr.addr('add',
                     index=self.interfaces[-1],
                     address='172.16.172.16',
                     prefixlen=24)
            self.addr_wait('172.16.172.16')
            ipr.route('add',
                      dst='172.16.127.0',
                      dst_len=24,
                      gateway='172.16.172.17')
            self.route_wait('172.16.127.0')

        iface = self.ndb.interfaces[self.if_simple]
        # check everything is in place
        assert grep('%s ip link show' % self.ssh, pattern=self.if_simple)
        assert grep('%s ip route show' % self.ssh, pattern=self.if_simple)
        assert grep('%s ip route show' % self.ssh,
                    pattern='172.16.127.*172.16.172.17')

        # remove the interface
        iface.remove()
        iface.commit()

        # check there is no interface, no route
        assert not grep('%s ip link show' % self.ssh, pattern=self.if_simple)
        assert not grep('%s ip route show' % self.ssh, pattern=self.if_simple)
        assert not grep('%s ip route show' % self.ssh,
                        pattern='172.16.127.*172.16.172.17')

        # revert the changes using the implicit last_save
        iface.rollback()
        assert grep('%s ip link show' % self.ssh, pattern=self.if_simple)
        assert grep('%s ip route show' % self.ssh, pattern=self.if_simple)
        assert grep('%s ip route show' % self.ssh,
                    pattern='172.16.127.*172.16.172.17')

    def test_bridge_deps(self):

        self.if_br0 = uifname()
        self.if_br0p0 = uifname()
        self.if_br0p1 = uifname()
        self.link_register(self.if_br0)
        self.link_register(self.if_br0p0)
        self.link_register(self.if_br0p1)
        self.addr_register('172.16.173.16')
        self.addr_register('172.16.173.17')
        self.route_register('172.16.128.0')

        with self.nl_class(**self.nl_kwarg) as ipr:
            self.interfaces = []
            ipr.link('add',
                     ifname=self.if_br0,
                     kind='bridge')
            self.interfaces.append(self.link_wait(self.if_br0))
            ipr.link('set',
                     index=self.interfaces[-1],
                     state='up')
            ipr.addr('add',
                     index=self.interfaces[-1],
                     address='172.16.173.16',
                     prefixlen=24)
            self.addr_wait('172.16.173.16')
            ipr.addr('add',
                     index=self.interfaces[-1],
                     address='172.16.173.17',
                     prefixlen=24)
            self.addr_wait('172.16.173.17')
            ipr.route('add',
                      dst='172.16.128.0',
                      dst_len=24,
                      gateway='172.16.173.18')
            self.route_wait('172.16.128.0')
            ipr.link('add',
                     ifname=self.if_br0p0,
                     kind='dummy')
            self.interfaces.append(self.link_wait(self.if_br0p0))
            ipr.link('set',
                     index=self.interfaces[-1],
                     state='up',
                     master=self.interfaces[-2])
            ipr.link('add',
                     ifname=self.if_br0p1,
                     kind='dummy')
            self.interfaces.append(self.link_wait(self.if_br0p1))
            ipr.link('set',
                     index=self.interfaces[-1],
                     state='up',
                     master=self.interfaces[-3])
        iface = self.ndb.interfaces[self.if_br0]
        # check everything is in place
        assert grep('%s ip link show' % self.ssh, pattern=self.if_br0)
        assert grep('%s ip link show' % self.ssh, pattern=self.if_br0p0)
        assert grep('%s ip link show' % self.ssh, pattern=self.if_br0p1)
        assert grep('%s ip addr show' % self.ssh, pattern='172.16.173.16')
        assert grep('%s ip addr show' % self.ssh, pattern='172.16.173.17')
        assert grep('%s ip route show' % self.ssh, pattern=self.if_br0)
        assert grep('%s ip route show' % self.ssh,
                    pattern='172.16.128.*172.16.173.18')

        # remove the interface
        iface.remove()
        iface.commit()

        # check there is no interface, no route
        assert not grep('%s ip link show' % self.ssh, pattern=self.if_br0)
        assert grep('%s ip link show' % self.ssh, pattern=self.if_br0p0)
        assert grep('%s ip link show' % self.ssh, pattern=self.if_br0p1)
        assert not grep('%s ip addr show' % self.ssh, pattern='172.16.173.16')
        assert not grep('%s ip addr show' % self.ssh, pattern='172.16.173.17')
        assert not grep('%s ip route show' % self.ssh, pattern=self.if_br0)
        assert not grep('%s ip route show' % self.ssh,
                        pattern='172.16.128.*172.16.173.18')

        # revert the changes using the implicit last_save
        iface.rollback()
        assert grep('%s ip link show' % self.ssh, pattern=self.if_br0)
        assert grep('%s ip link show' % self.ssh, pattern=self.if_br0p0)
        assert grep('%s ip link show' % self.ssh, pattern=self.if_br0p1)
        assert grep('%s ip addr show' % self.ssh, pattern='172.16.173.16')
        assert grep('%s ip addr show' % self.ssh, pattern='172.16.173.17')
        assert grep('%s ip route show' % self.ssh, pattern=self.if_br0)
        assert grep('%s ip route show' % self.ssh,
                    pattern='172.16.128.*172.16.173.18')

    def test_vlan_deps(self):

        if_host = uifname()
        if_vlan = uifname()
        self.link_register(if_host)
        self.link_register(if_vlan)

        with self.nl_class(**self.nl_kwarg) as ipr:
            self.interfaces = []
            ipr.link('add',
                     ifname=if_host,
                     kind='dummy')
            self.interfaces.append(self.link_wait(if_host))
            ipr.link('set',
                     index=self.interfaces[-1],
                     state='up')
            ipr.link('add',
                     ifname=if_vlan,
                     kind='vlan',
                     link=self.interfaces[-1],
                     vlan_id=1001)
            self.interfaces.append(self.link_wait(if_vlan))
            ipr.link('set',
                     index=self.interfaces[-1],
                     state='up')
            ipr.addr('add',
                     index=self.interfaces[-1],
                     address='172.16.174.16',
                     prefixlen=24)
            ipr.addr('add',
                     index=self.interfaces[-1],
                     address='172.16.174.17',
                     prefixlen=24)
            ipr.route('add',
                      dst='172.16.129.0',
                      dst_len=24,
                      gateway='172.16.174.18')

        iface = self.ndb.interfaces[if_host]
        # check everything is in place
        assert grep('%s ip link show' % self.ssh, pattern=if_host)
        assert grep('%s ip link show' % self.ssh, pattern=if_vlan)
        assert grep('%s ip addr show' % self.ssh, pattern='172.16.174.16')
        assert grep('%s ip addr show' % self.ssh, pattern='172.16.174.17')
        assert grep('%s ip route show' % self.ssh, pattern=if_vlan)
        assert grep('%s ip route show' % self.ssh,
                    pattern='172.16.129.*172.16.174.18')
        assert grep('%s cat /proc/net/vlan/config' % self.ssh, pattern=if_vlan)

        # remove the interface
        iface.remove()
        iface.commit()

        # check there is no interface, no route
        assert not grep('%s ip link show' % self.ssh, pattern=if_host)
        assert not grep('%s ip link show' % self.ssh, pattern=if_vlan)
        assert not grep('%s ip addr show' % self.ssh, pattern='172.16.174.16')
        assert not grep('%s ip addr show' % self.ssh, pattern='172.16.174.17')
        assert not grep('%s ip route show' % self.ssh, pattern=if_vlan)
        assert not grep('%s ip route show' % self.ssh,
                        pattern='172.16.129.*172.16.174.18')
        assert not grep('%s cat /proc/net/vlan/config' % self.ssh,
                        pattern=if_vlan)

        # revert the changes using the implicit last_save
        iface.rollback()
        assert grep('%s ip link show' % self.ssh, pattern=if_host)
        assert grep('%s ip link show' % self.ssh, pattern=if_vlan)
        assert grep('%s ip addr show' % self.ssh, pattern='172.16.174.16')
        assert grep('%s ip addr show' % self.ssh, pattern='172.16.174.17')
        assert grep('%s ip route show' % self.ssh, pattern=if_vlan)
        assert grep('%s ip route show' % self.ssh,
                    pattern='172.16.129.*172.16.174.18')
        assert grep('%s cat /proc/net/vlan/config' % self.ssh, pattern=if_vlan)


class TestSchema(TestBase):

    def test_basic(self):
        assert len(set(self.interfaces) -
                   set([x[0] for x in
                        self.fetch('select f_index from interfaces')])) == 0

    def test_vlan_interfaces(self):
        assert len(self.fetch('select * from vlan')) >= 2

    def test_bridge_interfaces(self):
        assert len(self.fetch('select * from bridge')) >= 1


class TestSources(TestBase):

    def count_interfaces(self, target):
        with self.ndb.schema.db_lock:
            return (self
                    .ndb
                    .schema
                    .execute('''
                             SELECT count(*) FROM interfaces
                             WHERE f_target = '%s'
                             ''' % target)
                    .fetchone()[0])

    def test_connect_netns(self):
        nsname = str(uuid.uuid4())
        with self.ndb.schema.db_lock:
            s = len(self.ndb.interfaces.summary()) - 1
            assert self.count_interfaces(nsname) == 0
            assert self.count_interfaces('localhost') == s

        # connect RTNL source
        event = threading.Event()
        self.ndb.connect_source(nsname, NetNS(nsname), event)
        assert event.wait(5)

        with self.ndb.schema.db_lock:
            s = len(self.ndb.interfaces.summary()) - 1
            assert self.count_interfaces(nsname) > 0
            assert self.count_interfaces('localhost') < s

        # disconnect the source
        self.ndb.disconnect_source(nsname)
        with self.ndb.schema.db_lock:
            s = len(self.ndb.interfaces.summary()) - 1
            assert self.count_interfaces(nsname) == 0
            assert self.count_interfaces('localhost') == s

        netns.remove(nsname)

    def test_disconnect_localhost(self):
        with self.ndb.schema.db_lock:
            s = len(self.ndb.interfaces.summary()) - 1
            assert self.count_interfaces('localhost') == s

        self.ndb.disconnect_source('localhost')

        with self.ndb.schema.db_lock:
            s = len(self.ndb.interfaces.summary()) - 1
            assert self.count_interfaces('localhost') == s
            assert s == 0


class TestReports(TestBase):

    def test_types(self):
        report.MAX_REPORT_LINES = 1
        # check for the report type here
        assert isinstance(self.ndb.interfaces.summary(), Report)
        # repr must be a string
        assert isinstance(repr(self.ndb.interfaces.summary()), basestring)
        # header + MAX_REPORT_LINES + (...)
        assert len(repr(self.ndb.interfaces.summary()).split('\n')) == 3

    def test_dump(self):
        for record in self.ndb.addresses.dump():
            assert isinstance(record, tuple)

    def test_csv(self):
        record_length = 0

        for record in self.ndb.routes.dump():
            if record_length == 0:
                record_length = len(record)
            else:
                assert len(record) == record_length

        for record in self.ndb.routes.csv():
            assert len(record.split(',')) == record_length
