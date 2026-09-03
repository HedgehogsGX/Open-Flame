from __future__ import annotations

import pytest

from video_download_control.security import (
    EgressPolicyError,
    assert_connected_peer,
    resolve_public_target,
    validate_redirect_chain,
)


def resolver_with(*answers: str):
    return lambda host, port: answers


def test_accepts_public_a_and_aaaa_and_deduplicates() -> None:
    target = resolve_public_target(
        "https://cdn.example.com/media",
        resolver=resolver_with(
            "8.8.8.8", "2606:4700:4700::1111", "8.8.8.8"
        ),
        allowed_hosts={"example.com"},
    )
    assert target.host == "cdn.example.com"
    assert target.port == 443
    assert target.addresses == ("8.8.8.8", "2606:4700:4700::1111")


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "100.64.0.1",
        "100.100.100.200",
        "169.254.170.2",
        "192.0.2.1",
        "224.0.0.1",
        "ff02::1",
        "fec0::1",
        "fd00:ec2::254",
    ],
)
def test_rejects_non_public_and_mapped_addresses(address: str) -> None:
    with pytest.raises(EgressPolicyError) as exc_info:
        resolve_public_target(
            "https://cdn.example.com/media",
            resolver=resolver_with(address),
        )
    assert exc_info.value.reason == "non_public_address"


def test_mixed_public_and_private_dns_answers_fail_closed() -> None:
    with pytest.raises(EgressPolicyError) as exc_info:
        resolve_public_target(
            "https://cdn.example.com/media",
            resolver=resolver_with("8.8.8.8", "127.0.0.1"),
        )
    assert exc_info.value.reason == "non_public_address"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("ftp://example.com/file", "invalid_scheme"),
        ("https://user:secret@example.com/file", "credentials_in_url"),
        ("https://@example.com/file", "credentials_in_url"),
        ("https://:@example.com/file", "credentials_in_url"),
        ("https://example.com:8443/file", "blocked_port"),
        ("https://example.com:80/file", "blocked_port"),
        ("http://example.com:443/file", "blocked_port"),
        ("https://127.0.0.1/file", "ip_literal_blocked"),
        ("https://localhost/file", "invalid_host"),
        ("https://bad_label.example/file", "invalid_host"),
        ("https://-bad.example/file", "invalid_host"),
        ("https://[fe80::1%25Ethernet]/file", "zone_identifier_blocked"),
    ],
)
def test_rejects_unsafe_target_shapes(url: str, reason: str) -> None:
    with pytest.raises(EgressPolicyError) as exc_info:
        resolve_public_target(url, resolver=resolver_with("8.8.8.8"))
    assert exc_info.value.reason == reason


def test_exact_or_subdomain_allowlist_does_not_accept_suffix_trick() -> None:
    resolve_public_target(
        "https://media.example.com/file",
        resolver=resolver_with("8.8.8.8"),
        allowed_hosts={"example.com"},
    )
    with pytest.raises(EgressPolicyError) as exc_info:
        resolve_public_target(
            "https://evilexample.com/file",
            resolver=resolver_with("8.8.8.8"),
            allowed_hosts={"example.com"},
        )
    assert exc_info.value.reason == "host_not_allowed"


def test_redirect_chain_revalidates_every_hop_and_final_domain() -> None:
    observed: list[str] = []

    def resolver(host: str, port: int):
        observed.append(f"{host}:{port}")
        return ["8.8.8.8"]

    targets = validate_redirect_chain(
        [
            "https://short.example/start",
            "https://redirect.example/path/next",
            "https://www.youtube.com/watch?v=test",
        ],
        resolver=resolver,
        final_allowed_hosts={"youtube.com"},
    )
    assert len(targets) == 3
    assert observed == [
        "short.example:443",
        "redirect.example:443",
        "www.youtube.com:443",
    ]

    relative_targets = validate_redirect_chain(
        ["https://redirect.example/path/start", "../watch?v=test"],
        resolver=resolver_with("8.8.8.8"),
        final_allowed_hosts={"redirect.example"},
    )
    assert relative_targets[-1].host == "redirect.example"

    with pytest.raises(EgressPolicyError) as wrong_final:
        validate_redirect_chain(
            ["https://short.example/start", "/watch?v=test"],
            resolver=resolver_with("8.8.8.8"),
            final_allowed_hosts={"youtube.com"},
        )
    assert wrong_final.value.reason == "final_host_not_allowed"

    with pytest.raises(EgressPolicyError) as exc_info:
        validate_redirect_chain(
            ["https://short.example/start", "https://evil.example/end"],
            resolver=resolver_with("8.8.8.8"),
            final_allowed_hosts={"youtube.com"},
        )
    assert exc_info.value.reason == "final_host_not_allowed"


def test_no_dns_answers_is_rejected() -> None:
    with pytest.raises(EgressPolicyError) as exc_info:
        resolve_public_target(
            "https://example.com/file", resolver=resolver_with()
        )
    assert exc_info.value.reason == "dns_no_answers"


def test_public_ip_literals_require_opt_in_and_do_not_use_dns() -> None:
    def resolver_must_not_run(host: str, port: int):
        raise AssertionError("literal must not be resolved again")

    ipv4 = resolve_public_target(
        "https://8.8.8.8/file",
        resolver=resolver_must_not_run,
        allow_ip_literal=True,
    )
    ipv6 = resolve_public_target(
        "https://[2606:4700:4700::1111]/file",
        resolver=resolver_must_not_run,
        allow_ip_literal=True,
    )
    assert ipv4.addresses == ("8.8.8.8",)
    assert ipv6.addresses == ("2606:4700:4700::1111",)


def test_mapped_public_answer_is_normalized_and_host_is_canonicalized() -> None:
    observed: list[tuple[str, int]] = []

    def resolver(host: str, port: int):
        observed.append((host, port))
        return ["::ffff:8.8.8.8"]

    target = resolve_public_target(
        "https://MEDIA.Example.COM./file",
        resolver=resolver,
        allowed_hosts={"example.com"},
    )
    assert target.host == "media.example.com"
    assert target.addresses == ("8.8.8.8",)
    assert observed == [("media.example.com", 443)]


def test_dns_failure_and_overlong_url_have_stable_safe_reasons() -> None:
    def failing_resolver(host: str, port: int):
        raise OSError("resolver failed for token=secret")

    with pytest.raises(EgressPolicyError) as dns_failure:
        resolve_public_target(
            "https://example.com/path?token=secret", resolver=failing_resolver
        )
    assert dns_failure.value.reason == "dns_failure"
    assert "secret" not in str(dns_failure.value)

    with pytest.raises(EgressPolicyError) as too_long:
        resolve_public_target(
            "https://example.com/" + ("a" * 4097),
            resolver=resolver_with("8.8.8.8"),
        )
    assert too_long.value.reason == "url_too_long"


def test_redirect_chain_rejects_loop_downgrade_and_limit() -> None:
    with pytest.raises(EgressPolicyError) as loop:
        validate_redirect_chain(
            ["https://example.com/a", "/a"],
            resolver=resolver_with("8.8.8.8"),
            final_allowed_hosts={"example.com"},
        )
    assert loop.value.reason == "redirect_loop"

    with pytest.raises(EgressPolicyError) as downgrade:
        validate_redirect_chain(
            ["https://example.com/a", "http://example.com/b"],
            resolver=resolver_with("8.8.8.8"),
            final_allowed_hosts={"example.com"},
        )
    assert downgrade.value.reason == "https_downgrade"

    with pytest.raises(EgressPolicyError) as limit:
        validate_redirect_chain(
            ["https://example.com/start"]
            + [f"https://example.com/{index}" for index in range(11)],
            resolver=resolver_with("8.8.8.8"),
            final_allowed_hosts={"example.com"},
        )
    assert limit.value.reason == "redirect_limit"


@pytest.mark.parametrize(
    ("location", "answers", "reason"),
    [
        ("https://user:secret@example.com/end", ("8.8.8.8",), "credentials_in_url"),
        ("https://example.com:8443/end", ("8.8.8.8",), "blocked_port"),
        ("https://metadata.example/end", ("169.254.169.254",), "non_public_address"),
        (
            "https://mixed.example/end",
            ("8.8.8.8", "127.0.0.1"),
            "non_public_address",
        ),
    ],
)
def test_redirect_chain_fails_closed_for_every_hop(
    location: str, answers: tuple[str, ...], reason: str
) -> None:
    def resolver(host: str, port: int):
        return ("8.8.8.8",) if host == "short.example" else answers

    with pytest.raises(EgressPolicyError) as exc_info:
        validate_redirect_chain(
            ["https://short.example/start", location],
            resolver=resolver,
            final_allowed_hosts={"example.com"},
        )
    assert exc_info.value.reason == reason


@pytest.mark.parametrize(
    "address",
    ["64:ff9b::808:808", "2001::1", "2002:0808:0808::1"],
)
def test_transition_addresses_are_rejected(address: str) -> None:
    with pytest.raises(EgressPolicyError) as exc_info:
        resolve_public_target(
            "https://example.com/file", resolver=resolver_with(address)
        )
    assert exc_info.value.reason == "transition_address_blocked"


def test_transport_peer_must_match_approved_dns_answer() -> None:
    target = resolve_public_target(
        "https://example.com/file", resolver=resolver_with("8.8.8.8")
    )
    assert_connected_peer(target, "8.8.8.8")
    with pytest.raises(EgressPolicyError) as exc_info:
        assert_connected_peer(target, "1.1.1.1")
    assert exc_info.value.reason == "peer_address_mismatch"
