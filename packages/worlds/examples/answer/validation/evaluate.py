"""Example deterministic evaluator; private answers never enter the learner image."""


def verify(context):
    answer = context.submission / "answer.txt"
    expected = (context.private / "expected.txt").read_text().strip()
    correct = answer.is_file() and answer.read_text().strip() == expected
    return {"score": float(correct), "metrics": {"correct": correct}}
