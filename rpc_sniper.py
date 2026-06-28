# rpc_sniper.py

import asyncio
import websockets
import json
import time
import logging
import os
import base64
from datetime import datetime
from dotenv import load_dotenv

from atomic_core import TradingWallet 
from target_fetcher import TargetFetcher

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("RPC_SNIPER")

load_dotenv()
PROOF_FILE = "simulation_proof.json"

class KaminoDecoder:
    def __init__(self):
        # [REDACTED FOR SECURITY] Proprietary memory offsets for Kamino V2 Obligation
        self.offset_deposited = 0
        self.offset_borrowed = 0
        
        
class RPCSniper:
    def __init__(self):
        self.ws = None
        self.current_node_index = 0
        self.last_slot = 0
        self.last_slot_time = time.time()
        self.is_blind = True
        self.ping_counter = 0 

        self.weapon = TradingWallet()
        self.decoder = KaminoDecoder()
        self.fetcher = TargetFetcher()
        self.targets = [] 

        self.nodes = [os.getenv("HELIUS_WSS"), os.getenv("QUICKNODE_WSS")]
        self.nodes = [n for node in self.nodes if node for n in [node]]
        
        if not self.nodes:
            logger.critical("[FATAL] No WSS keys found in .env")
            exit(1)

    def log_simulation_proof(self, target_pubkey, hf, deposit, borrow, verdict):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = {
            "timestamp": timestamp,
            "solana_slot": self.last_slot,
            "target_pubkey": target_pubkey,
            "health_factor": round(hf, 4) if hf else None,
            "simulated_deposit_value_raw": deposit,
            "simulated_borrow_value_raw": borrow,
            "blockchain_vm_verdict": str(verdict),
            "architecture_version": "7.5-Hybrid"
        }
        
        data = []
        if os.path.exists(PROOF_FILE):
            try:
                with open(PROOF_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = []
                
        data.append(entry)
        try:
            with open(PROOF_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            logger.info(f"[✓] Simulation record saved to {PROOF_FILE}")
        except Exception as e:
            logger.error(f"[!] Error writing Proof of Work: {e}")

    async def connect(self):
        node_url = self.nodes[self.current_node_index]
        provider_name = "HELIUS" if "helius" in node_url else "QUICKNODE"
        
        # Dynamic target extraction without hardware payload
        self.targets = self.fetcher.get_vulnerable_obligations()
        
        if not self.targets:
            logger.warning("[!] Target list empty. Retrying in 10 seconds...")
            await asyncio.sleep(10)
            await self.switch_node()
            return

        await self.weapon.initialize()
        
        logger.info(f"[*] Connecting to WSS channel {provider_name}...") 
        try:
            self.ws = await websockets.connect(node_url, ping_interval=None)
            self.is_blind = False
            self.last_slot_time = time.time() 
            logger.info(f"[✓] Sniper connected to block stream.")
            
            await self.subscribe_to_targets()
            
            listen_task = asyncio.create_task(self.listen())
            watchdog_task = asyncio.create_task(self.watchdog())
            
            await asyncio.gather(listen_task, watchdog_task)
            
        except Exception as e:
            logger.error(f"[!] Node failure {provider_name}: {e}")
            await self.switch_node()

    async def subscribe_to_targets(self):
        for i, target in enumerate(self.targets):
            req = {
                "jsonrpc": "2.0", 
                "id": f"sub_{i}", 
                "method": "accountSubscribe",
                "params": [target, {"encoding": "base64", "commitment": "processed"}]
            }
            await self.ws.send(json.dumps(req))
            
        slot_req = {"jsonrpc": "2.0", "id": "slot_sub", "method": "slotSubscribe"}
        await self.ws.send(json.dumps(slot_req))
        logger.info(f"[✓] Monitoring {len(self.targets)} Kamino accounts successfully started.")

    async def switch_node(self):
        self.is_blind = True
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.current_node_index = (self.current_node_index + 1) % len(self.nodes)
        logger.warning(f"[RECONNECT] Switching to backup node #{self.current_node_index}")
        await asyncio.sleep(2)
        await self.connect()

    async def watchdog(self):
        while not self.is_blind:
            await asyncio.sleep(1)
            if time.time() - self.last_slot_time > 5.0:
                logger.critical("[Х] WSS channel lag exceeded 5 seconds. Forced restart.")
                await self.switch_node()
                break

    async def listen(self):
        try:
            async for message in self.ws:
                data = json.loads(message)
                
                if data.get("method") == "slotNotification":
                    current_slot = data["params"]["result"]["slot"]
                    if current_slot > self.last_slot:
                        self.last_slot = current_slot
                        self.last_slot_time = time.time()
                        self.ping_counter += 1
                        if self.ping_counter % 10 == 0:
                            logger.info(f"[~] Network online. Current Solana slot: {self.last_slot}")
                
                # Real trigger for data changes on live Kamino accounts
                elif data.get("method") == "accountNotification":
                    try:
                        # Strict structure validation to bypass Helius load-balancer junk
                        params = data.get("params", {})
                        result = params.get("result", {})
                        value = result.get("value", {})
                        raw_data_array = value.get("data", [])
                        
                        if not raw_data_array or not isinstance(raw_data_array, list):
                            continue 
                            
                        pubkey = params.get("subscription", "UnknownTarget") 
                        raw_data = raw_data_array[0]
                        
                        hf, dep, bor = self.decoder.parse_health_factor(raw_data)
                        
                        if hf is not None:
                            logger.info(f"[*] Account state changed. Calculated Health Factor: {hf:.4f}")
                            
                            # If position is vulnerable — immediately simulate the bundle
                            if hf < 1.05:
                                logger.critical(f"[!!!] CRITICAL POSITION: HF={hf:.4f}. Pulling Core trigger.")
                                verdict = await self.weapon.simulate_ghost_transaction()
                                self.log_simulation_proof(pubkey, hf, dep, bor, verdict)
                                
                    except Exception:
                        pass
                        
        except websockets.exceptions.ConnectionClosed:
            await self.switch_node()
            
            
if __name__ == "__main__":
    sniper = RPCSniper()
    try:
        asyncio.run(sniper.connect())
    except KeyboardInterrupt:
        logger.info("[*] System stopped by user.")
