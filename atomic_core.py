#C:\Prozor_AI\core\atomic_core.py

import asyncio
import logging
import os
import re
import base64
import aiohttp
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.message import MessageV0
from solders.transaction import VersionedTransaction
from solders.system_program import TransferParams, transfer
from solders.address_lookup_table_account import AddressLookupTableAccount

# ==========================================
# ПАТЧ БЕЗОПАСНОСТИ: Универсальный маскиратор логов
# ==========================================
class SecureFormatter(logging.Formatter):
    KEY_REGEX = re.compile(r"([?&](?:api-key|key|token|auth)=)([a-zA-Z0-9_-]{3})([a-zA-Z0-9_-]+)", re.IGNORECASE)
    def format(self, record):
        original_msg = super().format(record)
        return self.KEY_REGEX.sub(r"\g<1>\g<2>***", original_msg)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setFormatter(SecureFormatter('%(asctime)s - %(levelname)s - %(message)s'))
if logger.hasHandlers():
    logger.handlers.clear()
logger.addHandler(console_handler)
logging.getLogger("httpx").setLevel(logging.INFO) # Радары сети включены

core_logger = logging.getLogger("ATOMIC_CORE")
load_dotenv()

MIN_SOL_GAS_LIMIT = 0.00  # Режим Призрака

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# ==========================================
# МОДУЛЬ NATIVE BUNDLING (Атомарный Компилятор)
# ==========================================
class NativeCompiler:
    def __init__(self, keypair, client):
        self.keypair = keypair
        self.client = client
        self.quote_url = "https://api.jup.ag/swap/v1/quote"
        # [ПАТЧ 7.5]: Обращаемся к скрытому эндпоинту за сырыми деталями
        self.swap_instr_url = "https://api.jup.ag/swap/v1/swap-instructions" 
        
        self.fee_wallet = os.getenv("PROZOR_FEE_WALLET")
        if not self.fee_wallet:
            core_logger.critical("[FATAL] Отсутствует PROZOR_FEE_WALLET в файле .env")
            exit(1)

    def _deserialize_instruction(self, instr_dict):
        """Парсер сырых JSON инструкций в объекты блокчейна (SVM)."""
        if not instr_dict:
            return None
        accounts = [
            AccountMeta(
                pubkey=Pubkey.from_string(acc["pubkey"]),
                is_signer=acc["isSigner"],
                is_writable=acc["isWritable"]
            ) for acc in instr_dict["accounts"]
        ]
        return Instruction(
            program_id=Pubkey.from_string(instr_dict["programId"]),
            data=base64.b64decode(instr_dict["data"]),
            accounts=accounts
        )

    async def build_and_compile(self):
        core_logger.info("[*] Запрос маршрута у Jupiter...")
        
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            # 1. Запрашиваем математику
            params = {
                "inputMint": SOL_MINT, "outputMint": USDC_MINT,
                "amount": "10000000", "slippageBps": "1"
            }
            async with session.get(self.quote_url, params=params) as resp:
                quote_data = await resp.json()

            if "error" in quote_data:
                core_logger.error(f"[!] Ошибка котировки: {quote_data}")
                return None

            # 2. Просим сырые инструкции для самостоятельной сборки
            payload = {
                "quoteResponse": quote_data,
                "userPublicKey": str(self.keypair.pubkey()),
                "wrapAndUnwrapSol": True,
            }
            async with session.post(self.swap_instr_url, json=payload) as resp:
                raw_data = await resp.json()

            if "error" in raw_data:
                core_logger.error(f"[!] Ошибка выдачи инструкций: {raw_data}")
                return None

            core_logger.info(">>> Детали обмена получены. Начинаем физическую сборку...")

            # 3. Распаковка таблиц маршрутизации (ALTs)
            alts = []
            alt_addresses = raw_data.get("addressLookupTableAddresses", [])
            if alt_addresses:
                core_logger.info(f"[*] Скачиваем {len(alt_addresses)} таблиц маршрутизации из блокчейна...")
                alt_pubkeys = [Pubkey.from_string(a) for a in alt_addresses]
                alt_resp = await self.client.get_multiple_accounts(alt_pubkeys)
                
                for i, acc_info in enumerate(alt_resp.value):
                    if acc_info:
                        # Побайтовый хак: вырезаем 56 байт заголовка, остальное - адреса пулов
                        data_bytes = acc_info.data
                        addresses = [Pubkey.from_bytes(data_bytes[offset:offset+32]) for offset in range(56, len(data_bytes), 32)]
                        alts.append(AddressLookupTableAccount(key=alt_pubkeys[i], addresses=addresses))

            # 4. Сборка массива инструкций
            all_instructions = []
            
            for inst in raw_data.get("setupInstructions", []):
                all_instructions.append(self._deserialize_instruction(inst))
                
            all_instructions.append(self._deserialize_instruction(raw_data["swapInstruction"]))

            # [ВШИВАНИЕ КОМИССИИ]: Инструкция Б
            core_logger.info(f"[*] Вшиваем системную Инструкцию Б (Перевод маржи в Казну)...")
            fee_pubkey = Pubkey.from_string(self.fee_wallet)
            
            # Для режима симуляции мы эмулируем перевод микро-доли SOL как комиссию
            fee_instruction = transfer(
                TransferParams(
                    from_pubkey=self.keypair.pubkey(),
                    to_pubkey=fee_pubkey,
                    lamports=1000000 # 0.001 SOL (Хватает для покрытия Rent)
                )
            )
            all_instructions.append(fee_instruction) # Пристегиваем к обмену

            if raw_data.get("cleanupInstruction"):
                all_instructions.append(self._deserialize_instruction(raw_data["cleanupInstruction"]))

            # 5. Запрос актуального хэша блока для криптографии
            blockhash_resp = await self.client.get_latest_blockhash()
            recent_blockhash = blockhash_resp.value.blockhash

            # 6. Финальная компиляция и запайка
            msg = MessageV0.try_compile(
                payer=self.keypair.pubkey(),
                instructions=all_instructions,
                address_lookup_table_accounts=alts,
                recent_blockhash=recent_blockhash,
            )
            
            tx = VersionedTransaction(msg, [self.keypair])
            core_logger.info("[✓] МОНОЛИТ СОБРАН: Обмен и Комиссия слиты в один атомарный бандл.")
            return tx


class TradingWallet:
    def __init__(self):
        self.rpc_url = os.getenv("HELIUS_HTTP")
        self.private_key_str = os.getenv("TRADING_PRIVATE_KEY")
        self.client = None
        self.keypair = None
        self.compiler = None

    async def initialize(self):
        self.client = AsyncClient(self.rpc_url)
        self.keypair = Keypair.from_base58_string(self.private_key_str)
        self.compiler = NativeCompiler(self.keypair, self.client)
        
        core_logger.info(f"[*] Изолированный кошелек загружен: {str(self.keypair.pubkey())[:3]}***")
        await self.audit_balances()

    async def audit_balances(self):
        await self.simulate_ghost_transaction()

    async def simulate_ghost_transaction(self):
        tx = await self.compiler.build_and_compile()
        if not tx: return "Error: Compilation Failed"

        core_logger.info("[*] Отправка Монолита на Симуляцию...")
        try:
            sim_resp = await self.client.simulate_transaction(tx)
            verdict = sim_resp.value.err
            core_logger.info(f">>> Вердикт Блокчейна: {verdict}")
            return verdict # КРИТИЧЕСКИ ВАЖНО: Возвращаем вердикт Снайперу для записи в лог
        except Exception as e:
            core_logger.error(f"[!] Ошибка симуляции: {e}")
            return f"Exception: {e}"


    async def shutdown(self):
        if self.client:
            await self.client.close()



#Версия с Подготовкой Ядра
#Открой atomic_core.py и спустись в самый низ. Удали блок async def main(): ... и if __name__ == "__main__": ....
#Они нам больше не нужны, так как этот файл больше не запускается сам по себе. Он теперь будет лежать в кобуре Снайпера.
