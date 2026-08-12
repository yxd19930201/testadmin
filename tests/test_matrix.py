"""核心比对逻辑冒烟测试：验证矩阵是确定性、不依赖 AI。"""
from captrace.checker import matrix as M


def test_consistent_text():
    m = M.build_matrix({"orderId": {"request": 88231, "response": 88231, "db": 88231, "log": 88231}})
    assert m[0].conclusion == "consistent"


def test_inconsistent_numeric_tolerance():
    # 99.00 与 99 视为一致（数值比较）
    m = M.build_matrix({"amount": {"request": 99.00, "response": 99}})
    assert m[0].conclusion == "consistent"


def test_inconsistent_detected():
    fields = {
        "amount": {"request": 99.00, "response": 99.00, "db": 99.00, "log": "expected 104.00"},
    }
    m = M.build_matrix(fields)
    assert m[0].conclusion == "inconsistent"
    assert M.has_anomaly(m) is True


def test_partial_when_empty():
    m = M.build_matrix({"x": {"request": None, "response": "", "db": " ", "log": None}})
    assert m[0].conclusion == "partial"


if __name__ == "__main__":
    test_consistent_text()
    test_inconsistent_numeric_tolerance()
    test_inconsistent_detected()
    test_partial_when_empty()
    print("ALL MATRIX TESTS PASSED")
