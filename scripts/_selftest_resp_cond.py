import sys, io
sys.path.insert(0, ".")
from captrace.app_gui import Api

a = Api()


# 复刻 _build_resp_conditions 的核心逻辑做独立验证
def norm_field_key(k):
    return a._norm_field_key(k)


def build(norm_cols, resp_row, table):
    table_norm = a._norm_key(table)
    table_bare = table_norm.split(".")[-1]
    pagination_like = set()
    id_candidates = ["id", f"{table_norm}id", f"{table_norm}_id", f"{table_bare}id", f"{table_bare}_id"]
    parts = [p for p in table_bare.split("_") if p]
    for p in parts:
        id_candidates.append(f"{p}id")
        id_candidates.append(f"{p}_id")
    conds, params, idx = [], {}, 0
    matched_field, norm_key = None, None
    for key in id_candidates:
        nk_cand = norm_field_key(key)
        if nk_cand in norm_cols:
            norm_key = nk_cand
            for k, v in resp_row.items():
                if v in (None, ""):
                    continue
                if norm_field_key(k) == norm_key:
                    matched_field = (k, v)
                    break
            if matched_field and norm_key in norm_cols:
                conds.append(f"{norm_cols[norm_key]} = :p{idx}")
                params[f"p{idx}"] = matched_field[1]
                idx += 1
                return conds, params, "id候选命中"
    # 兜底：出参里以 id 结尾的字段
    for k, v in resp_row.items():
        if v in (None, ""):
            continue
        nk = norm_field_key(k)
        if not nk.endswith("id"):
            continue
        if nk in norm_cols:
            conds.append(f"{norm_cols[nk]} = :p{idx}")
            params[f"p{idx}"] = v
            idx += 1
            return conds, params, "兜底id命中"
    return conds, params, "无命中"


# 场景1：库列就叫 euser_id
nc1 = {norm_field_key(c): c for c in ["euser_id", "company_name", "shop_type", "id"]}
r1 = build(nc1, {"euserId": 9, "companyName": "x", "shopType": None}, "master.euser_info")
print("场景1(列=euser_id):", r1)

# 场景2：库列叫 user_id
nc2 = {norm_field_key(c): c for c in ["user_id", "company_name", "shop_type", "id"]}
r2 = build(nc2, {"euserId": 9, "companyName": "x", "shopType": None}, "master.euser_info")
print("场景2(列=user_id):", r2)

# 场景3：库列就叫 id（值不是9，euserId对应不到id列名）
nc3 = {norm_field_key(c): c for c in ["id", "company_name", "shop_type"]}
r3 = build(nc3, {"euserId": 9, "companyName": "x", "shopType": None}, "master.euser_info")
print("场景3(列=id):", r3)
