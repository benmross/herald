"""Change delivery only after the owner approves the exact destinations."""
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


KINDS = {'purelymail.keep-copy': (_render, _perform)}
