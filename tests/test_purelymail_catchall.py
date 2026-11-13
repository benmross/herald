"""Catch-all writes preserve approved routing and refuse stale snapshots."""
import copy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
spec = importlib.util.spec_from_file_location('purelymail_actions', ROOT / 'extensions/purelymail/red.py')
actions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(actions)


class Catchall(unittest.TestCase):
    def setUp(self):
        self.rules = [dict(id=1, domainName='example.org', prefix=True, matchUser='',
                           targetAddresses=['copy@example.net'], catchall=False),
                      dict(id=2, domainName='example.org', prefix=False, matchUser='me',
                           targetAddresses=['me@example.org'], catchall=False)]
        self.payload = dict(domain='example.org', address='me@example.org',
                            expected_rules=copy.deepcopy(self.rules), expected_active_filters=[])
        for obj, name, kwargs in [
            (actions.red, 'acting', dict(return_value=123)),
            (actions.config, 'get', dict(return_value='me@example.org')),
            (actions.purelymail, 'routing_rules', dict(return_value=self.rules)),
            (actions.purelymail_sieve, 'connect', {}),
            (actions, '_route_api', {}),
        ]:
            p = patch.object(obj, name, **kwargs)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)
        self.connect.return_value.__enter__.return_value.scripts.return_value = []

    def test_no_approval_no_network(self):
        self.acting.return_value = None
        with self.assertRaises(PermissionError):
            actions._perform_catchall(self.payload)
        self.routing_rules.assert_not_called()
        self._route_api.assert_not_called()

    def test_changed_rules_refused(self):
        self.rules[0]['targetAddresses'] = ['changed@example.net']
        with self.assertRaisesRegex(RuntimeError, 'Routing changed'):
            actions._perform_catchall(self.payload)
        self._route_api.assert_not_called()

    def test_changed_filter_refused(self):
        self.payload['expected_active_filters'] = [{'name': 'old', 'script': 'keep;'}]
        with self.assertRaisesRegex(RuntimeError, 'filters changed'):
            actions._perform_catchall(self.payload)
        self._route_api.assert_not_called()

    def test_preserves_exact_rule(self):
        after = copy.deepcopy(self.rules)
        after[0].update(id=3, targetAddresses=['me@example.org'])
        self.routing_rules.side_effect = [self.rules, after]
        actions._perform_catchall(self.payload)
        self.assertEqual(self._route_api.call_args_list[0].args,
                         ('deleteRoutingRule', {'routingRuleId': 1}))
        self.assertEqual(self._route_api.call_args_list[1].args,
                         ('createRoutingRule', actions._route_fields(after[0])))

    def test_failed_creation_restores_old_when_absent(self):
        self.routing_rules.side_effect = [self.rules, self.rules[1:]]
        self._route_api.side_effect = [None, RuntimeError('create failed'), None]
        with self.assertRaisesRegex(RuntimeError, 'create failed'):
            actions._perform_catchall(self.payload)
        self.assertEqual(self._route_api.call_args_list[-1].args,
                         ('createRoutingRule', actions._route_fields(self.rules[0])))

    def test_uncertain_creation_does_not_overwrite_rule(self):
        after = copy.deepcopy(self.rules)
        after[0]['targetAddresses'] = ['me@example.org']
        self.routing_rules.side_effect = [self.rules, after]
        self._route_api.side_effect = [None, RuntimeError('response lost')]
        with self.assertRaisesRegex(RuntimeError, 'response lost'):
            actions._perform_catchall(self.payload)
        self.assertEqual(self._route_api.call_count, 2)


if __name__ == '__main__':
    unittest.main()
