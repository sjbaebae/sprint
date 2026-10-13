"""Validate and render failure spans without modifying retained trial records."""

import html

FIELDS = {"item.text", "item.command", "item.aggregated_output"}


def validate_annotations(events, annotations):
    """Resolve exact spans; reject ambiguous, overlapping or unsupported locations."""
    spans = {}
    ids = set()
    for finding in annotations:
        identifier = finding["id"]
        if not isinstance(identifier, str) or not identifier or identifier in ids:
            raise ValueError("Annotation IDs must be nonempty and unique per trial")
        ids.add(identifier)
        points = finding["points"]
        if type(points) is not int or not 1 <= points <= 10:
            raise ValueError("Points must be an integer from 1 to 10")
        for key in ("failure", "impact", "severity_reason"):
            if not isinstance(finding[key], str) or not finding[key].strip():
                raise ValueError(f"Missing {key}")
        evidence = finding["evidence"]
        if (
            not isinstance(evidence, list)
            or not evidence
            or any(not isinstance(x, str) or not x.strip() for x in evidence)
        ):
            raise ValueError("Evidence references are required")
        region = finding["region"]
        line, field, quote = region["event"], region["field"], region["quote"]
        if type(line) is not int or not 1 <= line <= len(events) or field not in FIELDS:
            raise ValueError("Invalid source event or field")
        value = events[line - 1].get("item", {}).get(field.split(".")[1])
        if not isinstance(value, str) or not isinstance(quote, str) or not quote:
            raise ValueError("Region must identify nonempty text")
        if "start" in region or "end" in region:
            start, end = region["start"], region["end"]
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= len(value)
            ):
                raise ValueError("Invalid character offsets")
        else:
            start = value.find(quote)
            if start < 0 or value.find(quote, start + 1) >= 0:
                raise ValueError(
                    "Quote must resolve uniquely; provide offsets for repeated text"
                )
            end = start + len(quote)
        if value[start:end] != quote:
            raise ValueError("Quote does not match source span")
        key = (line, field)
        existing = spans.setdefault(key, [])
        if any(start < b and a < end for a, b, _ in existing):
            raise ValueError("Overlapping failure regions must be consolidated")
        existing.append((start, end, identifier))
    return spans


def highlight(value, line, field, spans):
    """Escape all source text and mark only validated character ranges."""
    parts, cursor = [], 0
    for start, end, identifier in sorted(spans.get((line, field), [])):
        parts.append(html.escape(value[cursor:start]))
        parts.append(
            '<mark title="'
            + html.escape(identifier, quote=True)
            + '">'
            + html.escape(value[start:end])
            + "</mark>"
        )
        cursor = end
    parts.append(html.escape(value[cursor:]))
    return "".join(parts)


def notes(annotations, line):
    """Render explanations adjacent to their transcript event."""
    out = []
    for item in annotations:
        if item["region"]["event"] != line:
            continue

        def esc(value):
            return html.escape(str(value))

        title = item.get("title") or item["id"]
        if item.get("axis"):
            title = item["axis"].replace("_", " ").title() + " — " + title
        out.append(
            '<aside class="failure"><b>'
            + esc(title)
            + " · −"
            + str(item["points"])
            + " points</b><p>"
            + esc(item["failure"])
            + "</p><p>Impact: "
            + esc(item["impact"])
            + "</p><p>Severity: "
            + esc(item["severity_reason"])
            + "</p><p>Evidence: "
            + esc("; ".join(item["evidence"]))
            + "</p></aside>"
        )
    return "".join(out)
