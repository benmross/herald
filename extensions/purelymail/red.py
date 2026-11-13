"""Change delivery only after the owner approves the exact destinations."""
import json
import urllib.request

from herald import config, purelymail, purelymail_sieve, red


def _render(payload):
    return (f"Keep incoming mail in both accounts?\n\n"
            f"Mailbox: {payload['address']}\n"
            f"Forward a copy to: {payload['forward_to']}\n\n"
            "Add an exact routing rule for this mailbox and activate a new Sieve "
            "filter that forwards to the address above and keeps the local copy. "
            "Your existing catch-all remains in place for all other addresses. "
            "No existing routing rule or filter is deleted. This affects future mail only.")


def _perform(payload):
    if red.acting() is None:
        raise PermissionError('Routing changes require a tapped approval')
    address, target = payload['address'], payload['forward_to']
    if address != config.get('purelymail.address'):
        raise RuntimeError('Mailbox configuration changed; prepare a new approval')
    rules = purelymail.routing_rules()
    if rules != payload['expected_rules']:
        raise RuntimeError('Routing changed since preview; prepare a new approval')
    user, domain = address.split('@')
    if not any(r['domainName'] == domain and r['prefix'] and r['matchUser'] == ''
               and target in r['targetAddresses'] for r in rules):
        raise RuntimeError('The forwarding target must already be in your catch-all')
    if any(r['domainName'] == domain and not r['prefix'] and r['matchUser'] == user for r in rules):
        raise RuntimeError('An exact rule already exists; inspect it before changing delivery')
    script = 'redirect ' + purelymail_sieve.quote(target).decode() + ';\nkeep;\n'
    name = 'herald-forward-copy'
    with purelymail_sieve.connect() as client:
        scripts = client.scripts()
        if any(active or n == name for n, active in scripts):
            raise RuntimeError('A filter exists; preserve and review it before changing delivery')
        client.put(name, script)
        if client.get(name) != script:
            raise RuntimeError('Filter verification failed; routing was not changed')
        client.activate(name)
        if (name, True) not in client.scripts():
            raise RuntimeError('Filter activation failed; routing was not changed')
    # A self-targeting exact rule ends rewriting at this mailbox. It overrides
    # the broad redirect; Sieve then forwards and keeps a local copy.
    purelymail.create_local_route(user=user, domain=domain, address=address)
    expected = {'domainName': domain, 'prefix': False, 'matchUser': user,
                'targetAddresses': [address], 'catchall': False}
    if not any(all(r.get(k) == v for k, v in expected.items()) for r in purelymail.routing_rules()):
        raise RuntimeError('Routing was changed but could not be verified; inspect it before retrying')
    return (f'keep mail in {address} and forward a copy to {target}', f'purelymail:{address}')


def _render_catchall(p):
    return (f"Deliver all mail for @{p['domain']} to {p['address']}?\n\n"
            f"Replace the domain-wide forwarding rule with local delivery to {p['address']}. "
            "Apple Mail will receive it through your existing mailbox. "
            "Keep the exact mailbox rule and existing mailbox filters, including forwarding copies. "
            "This affects future mail only. If replacement fails and no new rule exists, "
            "attempt to restore the saved original rule.")


def _route_api(operation, payload):
    if red.acting() is None:
        raise PermissionError('Routing changes require a tapped approval')
    request = urllib.request.Request(
        'https://purelymail.com/api/v0/' + operation,
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json',
                 'Purelymail-Api-Token': config.secret('purelymail.api_key')})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if result.get('type') != 'success':
        raise RuntimeError('Purelymail routing operation failed: ' + operation)


def _route_fields(rule):
    return {k: rule[k] for k in
            ('domainName', 'prefix', 'matchUser', 'targetAddresses', 'catchall')}


def _perform_catchall(p):
    if red.acting() is None:
        raise PermissionError('Routing changes require a tapped approval')
    address, domain = p['address'], p['domain']
    if address != config.get('purelymail.address') or address.split('@')[1] != domain:
        raise RuntimeError('Mailbox configuration changed; prepare a new approval')
    rules = purelymail.routing_rules()
    if rules != p['expected_rules']:
        raise RuntimeError('Routing changed since preview; prepare a new approval')
    broad = [r for r in rules if r['domainName'] == domain
             and r['prefix'] and r['matchUser'] == '']
    if len(broad) != 1:
        raise RuntimeError('Expected exactly one existing domain-wide rule')
    old = broad[0]
    exact = {'domainName': domain, 'prefix': False, 'matchUser': address.split('@')[0],
             'targetAddresses': [address], 'catchall': False}
    if not any(_route_fields(r) == exact for r in rules):
        raise RuntimeError('An exact local mailbox rule must already exist')
    with purelymail_sieve.connect() as client:
        active = [{'name': n, 'script': client.get(n)} for n, a in client.scripts() if a]
    if active != p['expected_active_filters']:
        raise RuntimeError('Mailbox filters changed since preview; prepare a new approval')
    replacement = {**_route_fields(old), 'targetAddresses': [address]}
    if _route_fields(old) == replacement:
        raise RuntimeError('Catch-all already delivers locally')
    _route_api('deleteRoutingRule', {'routingRuleId': old['id']})
    try:
        _route_api('createRoutingRule', replacement)
    except Exception:
        # An interrupted response may still have created the replacement.
        # Restore only when readback proves the matching rule is absent.
        current = purelymail.routing_rules()
        same = [r for r in current if r['domainName'] == domain
                and r['prefix'] and r['matchUser'] == '']
        if not same:
            _route_api('createRoutingRule', _route_fields(old))
        raise
    current = purelymail.routing_rules()
    expected = [_route_fields(r) for r in rules if r['id'] != old['id']] + [replacement]
    canonical = lambda rows: sorted(json.dumps(r, sort_keys=True) for r in rows)
    if canonical([_route_fields(r) for r in current]) != canonical(expected):
        raise RuntimeError('Routing changed but verification failed; inspect before retrying')
    return (f'deliver all mail for @{domain} to {address}; preserve mailbox filters',
            f'purelymail:{domain}')


KINDS = {'purelymail.keep-copy': (_render, _perform),
         'purelymail.catchall-local': (_render_catchall, _perform_catchall)}
