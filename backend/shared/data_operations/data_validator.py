"""
标准化数据验证脚本

提供统一的数据质量检查和验证功能。
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pandas as pd

from .base import DatabaseDataOperation


@dataclass
class ValidationRule:
    """验证规则数据类"""

    name: str
    description: str
    validator_func: callable
    severity: str = "error"  # error, warning, info


@dataclass
class ValidationResult:
    """验证结果数据类"""

    rule_name: str
    passed: bool
    message: str
    details: dict[str, Any] | None = None
    severity: str = "error"


class DataValidator(DatabaseDataOperation):
    """
    标准化数据验证器

    功能：
    - 数据完整性检查
    - 数据一致性验证
    - 数据质量评估
    - 异常数据检测
    """

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__("data-validator", config)

        # 初始化验证规则
        self.validation_rules = self._initialize_validation_rules()

        # 默认配置
        self.default_config = {
            "max_null_percentage": 5.0,
            "min_data_completeness": 95.0,
            "price_change_threshold": 20.0,  # 价格变动阈值百分比
            "volume_anomaly_threshold": 5.0,  # 成交量异常阈值标准差
        }

        # 合并配置
        for key, value in self.default_config.items():
            if key not in self.config:
                self.config[key] = value

    def _execute_operation(self, **kwargs) -> dict[str, Any]:
        """
        执行数据验证操作

        Args:
            **kwargs: 操作参数
                - table_name: 表名（可选）
                - symbols: 股票代码列表（可选）
                - date_range: 日期范围（可选）
                - validation_types: 验证类型列表（可选）

        Returns:
            验证结果
        """
        validation_types = kwargs.get("validation_types", ["all"])

        # 连接数据库
        if not self.connect_to_database():
            return {"success": False, "error": "Failed to connect to database"}

        try:
            results = {}

            if "all" in validation_types or "completeness" in validation_types:
                results["completeness"] = self._validate_data_completeness(**kwargs)

            if "all" in validation_types or "consistency" in validation_types:
                results["consistency"] = self._validate_data_consistency(**kwargs)

            if "all" in validation_types or "quality" in validation_types:
                results["quality"] = self._validate_data_quality(**kwargs)

            if "all" in validation_types or "anomalies" in validation_types:
                results["anomalies"] = self._detect_anomalies(**kwargs)

            # 汇总结果
            total_validations = sum(len(result.get("validations", [])) for result in results.values())
            passed_validations = sum(
                sum(1 for v in result.get("validations", []) if v.passed) for result in results.values()
            )

            return {
                "success": True,
                "validation_summary": {
                    "total_validations": total_validations,
                    "passed_validations": passed_validations,
                    "failed_validations": total_validations - passed_validations,
                    "pass_rate": ((passed_validations / total_validations * 100) if total_validations > 0 else 0),
                },
                "results": results,
            }

        finally:
            self.close_database_connection()

    def _initialize_validation_rules(self) -> list[ValidationRule]:
        """初始化验证规则"""
        return [
            ValidationRule(
                name="no_null_prices",
                description="价格字段不能为空",
                validator_func=self._validate_no_null_prices,
                severity="error",
            ),
            ValidationRule(
                name="positive_prices",
                description="价格必须为正数",
                validator_func=self._validate_positive_prices,
                severity="error",
            ),
            ValidationRule(
                name="price_continuity",
                description="价格变动不能超过阈值",
                validator_func=self._validate_price_continuity,
                severity="warning",
            ),
            ValidationRule(
                name="volume_positive",
                description="成交量必须为非负数",
                validator_func=self._validate_volume_positive,
                severity="error",
            ),
            ValidationRule(
                name="date_sequence",
                description="日期序列必须连续",
                validator_func=self._validate_date_sequence,
                severity="warning",
            ),
            ValidationRule(
                name="ohlc_relationship",
                description="OHLC价格关系必须正确",
                validator_func=self._validate_ohlc_relationship,
                severity="error",
            ),
        ]

    def _validate_data_completeness(self, **kwargs) -> dict[str, Any]:
        """验证数据完整性"""
        self.logger.info("Validating data completeness", extra={"operation_id": self.operation_id})

        try:
            # 获取数据
            data = self._fetch_validation_data(**kwargs)

            if data.empty:
                return {
                    "success": False,
                    "message": "No data found for validation",
                    "validations": [],
                }

            validations = []

            # 检查空值比例
            null_percentage = (data.isnull().sum() / len(data) * 100).to_dict()
            max_null_pct = self.get_config_value("max_null_percentage", 5.0)

            for column, null_pct in null_percentage.items():
                if null_pct > max_null_pct:
                    validations.append(
                        ValidationResult(
                            rule_name="null_percentage",
                            passed=False,
                            message=f"Column {column} has {null_pct:.2f}% null values (threshold: {max_null_pct}%)",
                            details={"column": column, "null_percentage": null_pct},
                            severity="error",
                        )
                    )
                else:
                    validations.append(
                        ValidationResult(
                            rule_name="null_percentage",
                            passed=True,
                            message=f"Column {column} null percentage is acceptable",
                            details={"column": column, "null_percentage": null_pct},
                            severity="info",
                        )
                    )

            # 检查数据完整性
            completeness = (1 - data.isnull().sum().sum() / (len(data) * len(data.columns))) * 100
            min_completeness = self.get_config_value("min_data_completeness", 95.0)

            validations.append(
                ValidationResult(
                    rule_name="overall_completeness",
                    passed=completeness >= min_completeness,
                    message=f"Overall data completeness: {completeness:.2f}% (threshold: {min_completeness}%)",
                    details={
                        "completeness": completeness,
                        "threshold": min_completeness,
                    },
                    severity="error" if completeness < min_completeness else "info",
                )
            )

            return {
                "success": True,
                "message": "Data completeness validation completed",
                "validations": [self._serialize_validation(v) for v in validations],
            }

        except Exception as e:
            self.logger.error(
                "Data completeness validation failed",
                extra={"operation_id": self.operation_id, "error": str(e)},
            )
            return {
                "success": False,
                "message": f"Validation failed: {str(e)}",
                "validations": [],
            }

    def _validate_data_consistency(self, **kwargs) -> dict[str, Any]:
        """验证数据一致性"""
        self.logger.info("Validating data consistency", extra={"operation_id": self.operation_id})

        try:
            data = self._fetch_validation_data(**kwargs)

            if data.empty:
                return {
                    "success": False,
                    "message": "No data found for validation",
                    "validations": [],
                }

            validations = []

            # 应用所有验证规则
            for rule in self.validation_rules:
                try:
                    result = rule.validator_func(data)
                    if isinstance(result, list):
                        validations.extend(result)
                    else:
                        validations.append(result)
                except Exception as e:
                    validations.append(
                        ValidationResult(
                            rule_name=rule.name,
                            passed=False,
                            message=f"Rule execution failed: {str(e)}",
                            severity="error",
                        )
                    )

            return {
                "success": True,
                "message": "Data consistency validation completed",
                "validations": [self._serialize_validation(v) for v in validations],
            }

        except Exception as e:
            self.logger.error(
                "Data consistency validation failed",
                extra={"operation_id": self.operation_id, "error": str(e)},
            )
            return {
                "success": False,
                "message": f"Validation failed: {str(e)}",
                "validations": [],
            }

    def _validate_data_quality(self, **kwargs) -> dict[str, Any]:
        """验证数据质量"""
        self.logger.info("Validating data quality", extra={"operation_id": self.operation_id})

        try:
            data = self._fetch_validation_data(**kwargs)

            if data.empty:
                return {
                    "success": False,
                    "message": "No data found for validation",
                    "validations": [],
                }

            validations = []

            # 检查重复数据
            duplicates = data.duplicated().sum()
            validations.append(
                ValidationResult(
                    rule_name="no_duplicates",
                    passed=duplicates == 0,
                    message=f"Found {duplicates} duplicate records",
                    details={"duplicate_count": duplicates},
                    severity="warning",
                )
            )

            # 检查数据范围
            if "close" in data.columns:
                price_stats = data["close"].describe()
                validations.append(
                    ValidationResult(
                        rule_name="price_range",
                        passed=True,
                        message="Price range statistics",
                        details={
                            "min_price": price_stats["min"],
                            "max_price": price_stats["max"],
                            "mean_price": price_stats["mean"],
                            "std_price": price_stats["std"],
                        },
                        severity="info",
                    )
                )

            return {
                "success": True,
                "message": "Data quality validation completed",
                "validations": [self._serialize_validation(v) for v in validations],
            }

        except Exception as e:
            self.logger.error(
                "Data quality validation failed",
                extra={"operation_id": self.operation_id, "error": str(e)},
            )
            return {
                "success": False,
                "message": f"Validation failed: {str(e)}",
                "validations": [],
            }

    def _detect_anomalies(self, **kwargs) -> dict[str, Any]:
        """检测异常数据"""
        self.logger.info("Detecting data anomalies", extra={"operation_id": self.operation_id})

        try:
            data = self._fetch_validation_data(**kwargs)

            if data.empty:
                return {
                    "success": False,
                    "message": "No data found for anomaly detection",
                    "validations": [],
                }

            validations = []

            # 价格异常检测
            if "close" in data.columns:
                price_changes = data["close"].pct_change().dropna()
                threshold = self.get_config_value("price_change_threshold", 20.0) / 100

                anomalies = price_changes[abs(price_changes) > threshold]
                if not anomalies.empty:
                    validations.append(
                        ValidationResult(
                            rule_name="price_anomalies",
                            passed=False,
                            message=f"Found {len(anomalies)} price anomalies exceeding {threshold * 100:.1f}% change",
                            details={
                                "anomaly_count": len(anomalies),
                                "threshold": threshold,
                                "anomaly_dates": anomalies.index.tolist(),
                            },
                            severity="warning",
                        )
                    )

            # 成交量异常检测
            if "volume" in data.columns:
                volume_mean = data["volume"].mean()
                volume_std = data["volume"].std()
                threshold = self.get_config_value("volume_anomaly_threshold", 5.0)

                volume_anomalies = data[abs(data["volume"] - volume_mean) > threshold * volume_std]

                if not volume_anomalies.empty:
                    validations.append(
                        ValidationResult(
                            rule_name="volume_anomalies",
                            passed=False,
                            message=f"Found {len(volume_anomalies)} volume anomalies",
                            details={
                                "anomaly_count": len(volume_anomalies),
                                "threshold": threshold,
                                "volume_mean": volume_mean,
                                "volume_std": volume_std,
                            },
                            severity="warning",
                        )
                    )

            return {
                "success": True,
                "message": "Anomaly detection completed",
                "validations": [self._serialize_validation(v) for v in validations],
            }

        except Exception as e:
            self.logger.error(
                "Anomaly detection failed",
                extra={"operation_id": self.operation_id, "error": str(e)},
            )
            return {
                "success": False,
                "message": f"Anomaly detection failed: {str(e)}",
                "validations": [],
            }

    def _fetch_validation_data(self, **kwargs) -> pd.DataFrame:
        """获取用于验证的数据"""
        # 模拟数据获取
        dates = pd.date_range(start="2025-10-01", end="2025-10-10", freq="D")
        data = pd.DataFrame(
            {
                "date": dates,
                "symbol": "000001",
                "open": [10.0 + i * 0.1 for i in range(len(dates))],
                "high": [10.5 + i * 0.1 for i in range(len(dates))],
                "low": [9.5 + i * 0.1 for i in range(len(dates))],  # fidelity: allow-limit-threshold — 非阈值：mock 夹具构造价
                "close": [10.2 + i * 0.1 for i in range(len(dates))],
                "volume": [1000000 + i * 10000 for i in range(len(dates))],
            }
        )

        # 添加一些异常数据用于测试
        if len(data) > 5:
            data.loc[5, "close"] = 50.0  # 价格异常
            data.loc[6, "volume"] = 50000000  # 成交量异常

        return data

    # 验证函数
    def _validate_no_null_prices(self, data: pd.DataFrame) -> ValidationResult:
        """验证价格字段不能为空"""
        price_columns = ["open", "high", "low", "close"]
        null_prices = data[price_columns].isnull().any().any()

        return ValidationResult(
            rule_name="no_null_prices",
            passed=not null_prices,
            message=("Price fields contain null values" if null_prices else "Price fields are complete"),
            details={"null_columns": data[price_columns].columns[data[price_columns].isnull().any()].tolist()},
        )

    def _validate_positive_prices(self, data: pd.DataFrame) -> ValidationResult:
        """验证价格必须为正数"""
        price_columns = ["open", "high", "low", "close"]
        negative_prices = (data[price_columns] <= 0).any().any()

        return ValidationResult(
            rule_name="positive_prices",
            passed=not negative_prices,
            message=("Found non-positive prices" if negative_prices else "All prices are positive"),
            details={"negative_price_count": (data[price_columns] <= 0).sum().sum()},
        )

    def _validate_price_continuity(self, data: pd.DataFrame) -> ValidationResult:
        """验证价格变动不能超过阈值"""
        if "close" not in data.columns:
            return ValidationResult(
                rule_name="price_continuity",
                passed=True,
                message="No close price column found",
                severity="info",
            )

        threshold = self.get_config_value("price_change_threshold", 20.0) / 100
        price_changes = data["close"].pct_change().dropna()
        large_changes = abs(price_changes) > threshold

        return ValidationResult(
            rule_name="price_continuity",
            passed=not large_changes.any(),
            message=(
                f"Found {large_changes.sum()} large price changes"
                if large_changes.any()
                else "Price changes are within threshold"
            ),
            details={
                "threshold": threshold,
                "large_change_count": large_changes.sum(),
                "max_change": abs(price_changes).max(),
            },
        )

    def _validate_volume_positive(self, data: pd.DataFrame) -> ValidationResult:
        """验证成交量必须为非负数"""
        if "volume" not in data.columns:
            return ValidationResult(
                rule_name="volume_positive",
                passed=True,
                message="No volume column found",
                severity="info",
            )

        negative_volume = (data["volume"] < 0).any()

        return ValidationResult(
            rule_name="volume_positive",
            passed=not negative_volume,
            message=("Found negative volume values" if negative_volume else "All volume values are non-negative"),
            details={"negative_volume_count": (data["volume"] < 0).sum()},
        )

    def _validate_date_sequence(self, data: pd.DataFrame) -> ValidationResult:
        """验证日期序列必须连续"""
        if "date" not in data.columns:
            return ValidationResult(
                rule_name="date_sequence",
                passed=True,
                message="No date column found",
                severity="info",
            )

        # 检查日期是否有序
        dates_sorted = data["date"].is_monotonic_increasing

        return ValidationResult(
            rule_name="date_sequence",
            passed=dates_sorted,
            message=("Date sequence is not ordered" if not dates_sorted else "Date sequence is properly ordered"),
            details={"dates_sorted": dates_sorted},
        )

    def _validate_ohlc_relationship(self, data: pd.DataFrame) -> ValidationResult:
        """验证OHLC价格关系必须正确"""
        required_columns = ["open", "high", "low", "close"]
        missing_columns = [col for col in required_columns if col not in data.columns]

        if missing_columns:
            return ValidationResult(
                rule_name="ohlc_relationship",
                passed=False,
                message=f"Missing OHLC columns: {missing_columns}",
                severity="error",
            )

        # 检查 high >= max(open, close) 和 low <= min(open, close)
        invalid_high = data["high"] < data[["open", "close"]].max(axis=1)
        invalid_low = data["low"] > data[["open", "close"]].min(axis=1)

        invalid_records = invalid_high | invalid_low

        return ValidationResult(
            rule_name="ohlc_relationship",
            passed=not invalid_records.any(),
            message=(
                f"Found {invalid_records.sum()} records with invalid OHLC relationships"
                if invalid_records.any()
                else "OHLC relationships are valid"
            ),
            details={"invalid_record_count": invalid_records.sum()},
        )

    def _serialize_validation(self, validation: ValidationResult) -> dict[str, Any]:
        """序列化验证结果"""
        return {
            "rule_name": validation.rule_name,
            "passed": validation.passed,
            "message": validation.message,
            "details": validation.details,
            "severity": validation.severity,
        }


# 便捷函数
def validate_stock_data(
    validation_types: list[str] = ["all"],
    symbols: list[str] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    便捷的股票数据验证函数

    Args:
        validation_types: 验证类型列表
        symbols: 股票代码列表
        config: 配置字典

    Returns:
        验证结果
    """
    validator = DataValidator(config)
    return validator.execute(validation_types=validation_types, symbols=symbols)


if __name__ == "__main__":
    # 示例用法
    result = validate_stock_data(validation_types=["completeness", "consistency"])
    print("Data validation result:", result)
