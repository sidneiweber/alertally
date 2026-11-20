def rule_matches(labels, rule):
    """A rule matches when every one of its labels has the same value in the alert."""
    return isinstance(labels, dict) and all(labels.get(key) == value for key, value in rule.items())


def matching_services(alert, services):
    labels = alert.get('labels')
    return [service for service in services
            if any(rule_matches(labels, rule) for rule in service.get('rules', []))]
