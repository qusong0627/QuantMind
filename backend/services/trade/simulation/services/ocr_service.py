import base64
import json
import logging
import re
import os
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI
from backend.services.trade.trade_config import settings
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

class SimulationOCRService:
    """
    Service for recognizing stock holdings from images using Qwen-VL.
    """

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize the OCR service.
        :param api_key: Optional user-provided API key from profile.
        """
        effective_key = api_key or getattr(settings, "DASHSCOPE_API_KEY", None) or os.getenv("DASHSCOPE_API_KEY")
        
        if not effective_key or effective_key == "mock-api-key-not-configured":
            logger.warning("No valid DASHSCOPE_API_KEY found. OCR functionality will be limited to local fallbacks if any.")
            self.client = None
        else:
            self.client = AsyncOpenAI(
                api_key=effective_key,
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            )
        
        self.model_name = "qwen-vl-max" # Or qwen-vl-max for better performance

    async def analyze_images(self, image_data_list: List[bytes]) -> List[Dict[str, Any]]:
        """
        Analyze a list of images and extract stock holding information.
        """
        if not self.client:
            raise ValueError("OCR Service is not configured (missing API Key)")

        all_results = []
        
        for image_bytes in image_data_list:
            try:
                base64_image = base64.b64encode(image_bytes).decode("utf-8")
                
                prompt = (
                    "你是一个专业的金融数据提取助手。请从图片中提取股票持仓信息。\n"
                    "要求：\n"
                    "1. 只需要准确提取【股票名称】、【持仓数量】、【当前市价】。不要提取股票代码。\n"
                    "2. 数量和当前市价都请转换为纯数字，去掉逗号。\n"
                    "3. 输出格式为 JSON 数组，字段：'name', 'quantity', 'current_price'。\n"
                    "注意：请务必仔细分辨数字，如 15600 不要识别成 1560。"
                )

                # OpenAI client in dashscope compatible mode
                completion = await self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/jpeg;base64,{base64_image}"
                                    },
                                },
                                {"type": "text", "text": prompt},
                            ],
                        },
                    ],
                )
                
                content = completion.choices[0].message.content
                logger.debug(f"Qwen-VL response: {content}")
                
                # Extract JSON from potential markdown markers
                json_str = content
                if "```json" in content:
                    match = re.search(r"```json\s*(.*?)\s*```", content, re.DOTALL)
                    if match:
                        json_str = match.group(1)
                elif "```" in content:
                    match = re.search(r"```\s*(.*?)\s*```", content, re.DOTALL)
                    if match:
                        json_str = match.group(1)
                
                try:
                    items = json.loads(json_str)
                except json.JSONDecodeError:
                    # Fallback: try to find anything that looks like a JSON array
                    match = re.search(r"\[.*\]", json_str, re.DOTALL)
                    if match:
                        items = json.loads(match.group(0))
                    else:
                        raise

                if isinstance(items, list):
                    for item in items:
                        # Normalize stock code
                        raw_code = str(item.get("code", "")).strip()
                        name = str(item.get("name", "")).strip()
                        quantity_raw = item.get("quantity")
                        current_price_raw = item.get("current_price")
                        
                        if not raw_code and not name:
                            continue
                            
                        # Convert quantity to float
                        try:
                            if isinstance(quantity_raw, str):
                                quantity = float(quantity_raw.replace(",", ""))
                            else:
                                quantity = float(quantity_raw or 0)
                        except (ValueError, TypeError):
                            quantity = 0.0

                        try:
                            if isinstance(current_price_raw, str):
                                current_price = float(current_price_raw.replace(",", ""))
                            else:
                                current_price = float(current_price_raw or 0)
                        except (ValueError, TypeError):
                            current_price = 0.0

                        symbol = StockCodeUtil.to_prefix(raw_code) if raw_code else None
                        
                        all_results.append({
                            "symbol": symbol,
                            "name": name,
                            "quantity": quantity
                            ,"current_price": current_price
                        })
                
            except Exception as e:
                logger.error(f"Failed to analyze holding image: {e}", exc_info=True)
                # Continue with next image instead of failing entirely
                
        # Deduplicate and merge quantities for same symbol or name
        merged = {}
        for r in all_results:
            key = r["symbol"] or r["name"]
            if not key:
                continue
                
            if key in merged:
                merged[key]["quantity"] += r["quantity"]
            else:
                merged[key] = r
                
        return list(merged.values())
