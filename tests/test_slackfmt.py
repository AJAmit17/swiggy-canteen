from canteen.slackfmt import to_mrkdwn


def test_double_asterisk_bold_becomes_single_asterisk():
    assert to_mrkdwn("**HOSTEL** is the one") == "*HOSTEL* is the one"


def test_underscore_bold_also_becomes_slack_bold():
    assert to_mrkdwn("__HOSTEL__") == "*HOSTEL*"


def test_single_asterisks_are_left_alone():
    """Regression: the model writes Slack dialect, where *x* is already bold.
    Rewriting it to _x_ silently downgraded every bold heading to italic."""
    assert to_mrkdwn("*HOSTEL* is the one") == "*HOSTEL* is the one"


def test_underscore_italics_pass_through():
    assert to_mrkdwn("that was _quick_") == "that was _quick_"


def test_converting_twice_changes_nothing():
    """Output is Slack dialect, so feeding it back in must be a no-op."""
    once = to_mrkdwn("**Toit** and _maybe_ Sattvik\n- Dal")
    assert to_mrkdwn(once) == once


def test_headings_become_bold_lines():
    assert to_mrkdwn("### Your addresses\ntext") == "*Your addresses*\ntext"


def test_links_become_slack_angle_bracket_form():
    assert to_mrkdwn("[Toit](https://toit.in)") == "<https://toit.in|Toit>"


def test_bare_urls_are_left_alone():
    assert to_mrkdwn("see https://toit.in ok") == "see https://toit.in ok"


def test_bullets_become_real_bullets():
    assert to_mrkdwn("- Dal\n* Roti\n+ Rice") == "• Dal\n• Roti\n• Rice"


def test_numbered_lists_are_left_alone():
    assert to_mrkdwn("1. Dal\n2. Roti") == "1. Dal\n2. Roti"


def test_strikethrough_loses_one_tilde():
    assert to_mrkdwn("~~closed~~") == "~closed~"


def test_code_spans_are_never_rewritten():
    """An address id in backticks must survive verbatim — underscores and
    asterisks inside code are data, not formatting."""
    assert to_mrkdwn("id `a_b_c` and `**raw**`") == "id `a_b_c` and `**raw**`"


def test_fenced_code_blocks_are_never_rewritten():
    src = "```\n- not a bullet\n**not bold**\n```"
    assert to_mrkdwn(src) == src


def test_a_real_model_reply_converts_cleanly():
    raw = (
        "Here are your saved addresses:\n\n"
        "1. **HOSTEL** (ID: `clgo9g9g0aucvgf77msg`) – Bhoganhalli\n"
        "2. **Home** (ID: `123123770`) – Gangavati\n\n"
        "Which one?"
    )
    assert to_mrkdwn(raw) == (
        "Here are your saved addresses:\n\n"
        "1. *HOSTEL* (ID: `clgo9g9g0aucvgf77msg`) – Bhoganhalli\n"
        "2. *Home* (ID: `123123770`) – Gangavati\n\n"
        "Which one?"
    )


def test_empty_and_plain_text_pass_through():
    assert to_mrkdwn("") == ""
    assert to_mrkdwn("just words") == "just words"
