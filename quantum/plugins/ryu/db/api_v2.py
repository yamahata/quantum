# vim: tabstop=4 shiftwidth=4 softtabstop=4
# Copyright 2012 Isaku Yamahata <yamahata at private email ne jp>
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import logging

from sqlalchemy import exc as sa_exc
from sqlalchemy import func
from sqlalchemy.orm import exc as orm_exc

from quantum.common import exceptions as q_exc
import quantum.db.api as db
from quantum.db import models_v2
from quantum.plugins.ryu.db import models_v2 as ryu_models_v2


LOG = logging.getLogger(__name__)


def set_ofp_servers(hosts):
    session = db.get_session()
    session.query(ryu_models_v2.OFPServer).delete()
    for (host_address, host_type) in hosts:
        host = ryu_models_v2.OFPServer(address=host_address,
                                       host_type=host_type)
        session.add(host)
    session.flush()


def network_all_tenant_list():
    session = db.get_session()
    return session.query(models_v2.Network).all()


# VLAN: 12 bits
# GRE, VXLAN: 24bits
# TODO(yamahata): STT: 64bits
_TUNNEL_KEY_MIN_HARD = 1
_TUNNEL_KEY_MAX_HARD = 0xffffffff
_TUNNEL_KEY_MIN = 1
_TUNNEL_KEY_MAX = 0xffffffff


def tunnel_key_init(tunnel_key_min, tunnel_key_max):
    if (tunnel_key_min < _TUNNEL_KEY_MIN_HARD or
        tunnel_key_max > _TUNNEL_KEY_MAX_HARD or
            tunnel_key_min > tunnel_key_max):
        LOG.warn('Invalid tunnel key options '
                 'tunnel_key_min: %d tunnel_key_max: %d. '
                 'Using default value', tunnel_key_min, tunnel_key_min)
        return

    global _TUNNEL_KEY_MIN
    global _TUNNEL_KEY_MAX
    _TUNNEL_KEY_MIN = tunnel_key_min
    _TUNNEL_KEY_MAX = tunnel_key_max


def _tunnel_key_last(session):
    try:
        return session.query(ryu_models_v2.TunnelKeyLast).one()
    except orm_exc.MultipleResultsFound:
        max_key = session.query(func.max(ryu_models_v2.TunnelKeyLast.last_key))
        if max_key > _TUNNEL_KEY_MAX:
            max_key = _TUNNEL_KEY_MIN

        session.query(ryu_models_v2.TunnelKeyLast).delete()
        last_key = ryu_models_v2.TunnelKeyLast(last_key=max_key)
    except orm_exc.NoResultFound:
        last_key = ryu_models_v2.TunnelKeyLast(last_key=_TUNNEL_KEY_MIN)

    session.add(last_key)
    session.flush()
    return session.query(ryu_models_v2.TunnelKeyLast).one()


def _tunnel_key_find(session, last_key):
    """
    Try to find unused tunnel key in TunnelKey table starting
    from last_key + 1.
    When all keys are used, raise sqlalchemy.orm.exc.NoResultFound
    """
    # key 0 is used for special meanings. So don't allocate 0.

    # sqlite doesn't support
    # '(select order by limit) union all (select order by limit) '
    # 'order by limit'
    # So do it manually
    # new_key = session.query("new_key").from_statement(
    #     # If last_key + 1 isn't used, it's the result
    #     'SELECT new_key '
    #     'FROM (SELECT :last_key + 1 AS new_key) q1 '
    #     'WHERE NOT EXISTS '
    #     '(SELECT 1 FROM tunnelkeys WHERE tunnel_key = :last_key + 1) '
    #
    #     'UNION ALL '
    #
    #     # if last_key + 1 used, find the least unused key from last_key + 1
    #     '(SELECT t.tunnel_key + 1 AS new_key '
    #     'FROM tunnelkeys t '
    #     'WHERE NOT EXISTS '
    #     '(SELECT 1 FROM tunnelkeys ti '
    #     ' WHERE ti.tunnel_key = t.tunnel_key + 1) '
    #     'AND t.tunnel_key >= :last_key '
    #     'ORDER BY new_key LIMIT 1) '
    #
    #     'ORDER BY new_key LIMIT 1'
    # ).params(last_key=last_key).one()
    try:
        new_key = session.query("new_key").from_statement(
            # If last_key + 1 isn't used, it's the result
            'SELECT new_key '
            'FROM (SELECT :last_key + 1 AS new_key) q1 '
            'WHERE NOT EXISTS '
            '(SELECT 1 FROM tunnelkeys WHERE tunnel_key = :last_key + 1) '
        ).params(last_key=last_key).one()
    except orm_exc.NoResultFound:
        new_key = session.query("new_key").from_statement(
            # if last_key + 1 used, find the least unused key from last_key + 1
            '(SELECT t.tunnel_key + 1 AS new_key '
            'FROM tunnelkeys t '
            'WHERE NOT EXISTS '
            '(SELECT 1 FROM tunnelkeys ti '
            ' WHERE ti.tunnel_key = t.tunnel_key + 1) '
            'AND t.tunnel_key >= :last_key '
            'ORDER BY new_key LIMIT 1) '
        ).params(last_key=last_key).one()

    new_key = new_key[0]  # the result is tuple.
    LOG.debug("last_key %s new_key %s", last_key, new_key)
    if new_key > _TUNNEL_KEY_MAX:
        LOG.debug("no key found")
        raise orm_exc.NoResultFound
    return new_key


def _tunnel_key_allocate(session, network_id):
    last_key = _tunnel_key_last(session)
    try:
        new_key = _tunnel_key_find(session, last_key.last_key)
    except orm_exc.NoResultFound:
        new_key = _tunnel_key_find(session, _TUNNEL_KEY_MIN)

    tunnel_key = ryu_models_v2.TunnelKey(network_id=network_id,
                                         tunnel_key=new_key)
    last_key.last_key = new_key
    session.add(tunnel_key)
    return new_key


_TRANSACTION_RETRY_MAX = 16


def tunnel_key_allocate(session, network_id):
    count = 0
    while True:
        session.begin(subtransactions=True)
        try:
            new_key = _tunnel_key_allocate(session, network_id)
            session.commit()
            break
        except sa_exc.SQLAlchemyError:
            session.rollback()

        count += 1
        if count > _TRANSACTION_RETRY_MAX:
            # if this happens too often, increase _TRANSACTION_RETRY_MAX
            LOG.warn("Transaction retry reaches to %d. "
                     "abandan to allocate tunnel key." % count)
            raise q_exc.ResourceExhausted()

    return new_key


def tunnel_key_delete(session, network_id):
    session.query(ryu_models_v2.TunnelKey).filter_by(
        network_id=network_id).delete()
    session.flush()


def tunnel_key_all_list():
    session = db.get_session()
    return session.query(ryu_models_v2.TunnelKey).all()
