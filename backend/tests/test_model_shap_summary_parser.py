from pathlib import Path

from backend.services.api.training_shap_summary import read_shap_summary_rows


def test_read_shap_summary_rows_sorts_by_mean_abs_shap(tmp_path: Path) -> None:
    csv_path = tmp_path / "shap_summary.csv"
    csv_path.write_text(
        "\n".join(
            [
                "feature,mean_abs_shap,mean_shap,positive_ratio",
                "alpha_a,0.12,0.03,0.55",
                "alpha_b,0.80,-0.10,0.31",
                "alpha_c,0.40,0.04,0.72",
            ]
        ),
        encoding="utf-8",
    )

    rows = read_shap_summary_rows(csv_path)
    assert [item["feature"] for item in rows] == ["alpha_b", "alpha_c", "alpha_a"]
    assert [item["rank"] for item in rows] == [1, 2, 3]
    assert rows[0]["mean_abs_shap"] == 0.80
    assert rows[1]["mean_shap"] == 0.04


def test_read_shap_summary_rows_ignores_blank_feature(tmp_path: Path) -> None:
    csv_path = tmp_path / "shap_summary.csv"
    csv_path.write_text(
        "\n".join(
            [
                "feature,mean_abs_shap,mean_shap,positive_ratio",
                ",0.12,0.03,0.55",
                "alpha_x,not_num,1.0,0.5",
            ]
        ),
        encoding="utf-8",
    )

    rows = read_shap_summary_rows(csv_path)
    assert len(rows) == 1
    assert rows[0]["feature"] == "alpha_x"
    assert rows[0]["mean_abs_shap"] == 0.0
