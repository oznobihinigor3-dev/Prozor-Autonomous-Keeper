# atomic_core.py

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
# SECURITY PATCH: Universal log masker
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
logging.getLogger("httpx").setLevel(logging.INFO) # Network radars active

core_logger = logging.getLogger("ATOMIC_CORE")
load_dotenv()

MIN_SOL_GAS_LIMIT = 0.00  # Ghost Mode

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# ==========================================
# NATIVE BUNDLING MODULE (Atomic Compiler)
# ==========================================
class NativeCompiler:
    def __init__(self, keypair, client):
        self.keypair = keypair
        self.client = client
        self.quote_url = "https://api.jup.ag/swap/v1/quote"
        # [PATCH 7.5]: Querying hidden endpoint for raw swap instructions
        self.swap_instr_url = "https://api.jup.ag/swap/v1/swap-instructions" 
        
        self.fee_wallet = os.getenv("PROZOR_FEE_WALLET")
        if not self.fee_wallet:
            core_logger.critical("[FATAL] Missing PROZOR_FEE_WALLET in .env")
            exit(1)

    def _deserialize_instruction(self, instr_dict):
        """Parses raw JSON instructions into SVM objects."""
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
        core_logger.info("[*] Requesting route from Jupiter...")
        
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            # 1. Request routing math
            params = {
                "inputMint": SOL_MINT, "outputMint": USDC_MINT,
                "amount": "10000000", "slippageBps": "1"
            }
            async with session.get(self.quote_url, params=params) as resp:
                quote_data = await resp.json()

            if "error" in quote_data:
                core_logger.error(f"[!] Quote error: {quote_data}")
                return None

            # 2. Request raw instructions for custom assembly
            payload = {
                "quoteResponse": quote_data,
                "userPublicKey": str(self.keypair.pubkey()),
                "wrapAndUnwrapSol": True,
            }
            async with session.post(self.swap_instr_url, json=payload) as resp:
                raw_data = await resp.json()

            if "error" in raw_data:
                core_logger.error(f"[!] Instruction delivery error: {raw_data}")
                return None

            core_logger.info(">>> Swap details received. Starting physical assembly...")

            # 3. Extracting Address Lookup Tables (ALTs)
            alts = []
            alt_addresses = raw_data.get("addressLookupTableAddresses", [])
            if alt_addresses:
                core_logger.info(f"[*] Downloading {len(alt_addresses)} routing tables from blockchain...")
                alt_pubkeys = [Pubkey.from_string(a) for a in alt_addresses]
                alt_resp = await self.client.get_multiple_accounts(alt_pubkeys)
                
                for i, acc_info in enumerate(alt_resp.value):
                    if acc_info:
                        # Byte-level hack: slice 56-byte header, extract 32-byte pool addresses
                        data_bytes = acc_info.data
                        addresses = [Pubkey.from_bytes(data_bytes[offset:offset+32]) for offset in range(56, len(data_bytes), 32)]
                        alts.append(AddressLookupTableAccount(key=alt_pubkeys[i], addresses=addresses))

            # 4. Assembling instruction array
            all_instructions = []
            
            for inst in raw_data.get("setupInstructions", []):
                all_instructions.append(self._deserialize_instruction(inst))
                
            all_instructions.append(self._deserialize_instruction(raw_data["swapInstruction"]))

            # [FEE INJECTION]: Instruction B
            core_logger.info(f"[*] Injecting system Instruction B (Margin transfer to Treasury)...")
            fee_pubkey = Pubkey.from_string(self.fee_wallet)
            
            # For simulation mode, emulate a micro-SOL transfer to verify margin capacity
            fee_instruction = transfer(
                TransferParams(
                    from_pubkey=self.keypair.pubkey(),
                    to_pubkey=fee_pubkey,
                    lamports=1000000 # 0.001 SOL (Rent exemption coverage)
                )
            )
            all_instructions.append(fee_instruction)

            if raw_data.get("cleanupInstruction"):
                all_instructions.append(self._deserialize_instruction(raw_data["cleanupInstruction"]))

            # 5. Fetch recent blockhash for cryptography
            blockhash_resp = await self.client.get_latest_blockhash()
            recent_blockhash = blockhash_resp.value.blockhash

            # 6. Final compilation and sealing
            msg = MessageV0.try_compile(
                payer=self.keypair.pubkey(),
                instructions=all_instructions,
                address_lookup_table_accounts=alts,
                recent_blockhash=recent_blockhash,
            )
            
            tx = VersionedTransaction(msg, [self.keypair])
            core_logger.info("[✓] MONOLITH ASSEMBLED: Swap and Fee merged into a single atomic bundle.")
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
        
        core_logger.info(f"[*] Isolated wallet loaded: {str(self.keypair.pubkey())[:3]}***")
        await self.audit_balances()

    async def audit_balances(self):
        await self.simulate_ghost_transaction()

    async def simulate_ghost_transaction(self):
        tx = await self.compiler.build_and_compile()
        if not tx: return "Error: Compilation Failed"

        core_logger.info("[*] Sending Monolith for Simulation...")
        try:
            sim_resp = await self.client.simulate_transaction(tx)
            verdict = sim_resp.value.err
            core_logger.info(f">>> Blockchain Verdict: {verdict}")
            return verdict # CRITICAL: Return verdict to Sniper for logging
        except Exception as e:
            core_logger.error(f"[!] Simulation error: {e}")
            return f"Exception: {e}"


    async def shutdown(self):
        if self.client:
            await self.client.close()
