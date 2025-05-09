import asyncio
import aiohttp
import json
import logging
import uuid  # Added for unique address
import websockets  # Added for exception handling
from asyncio import Queue # Added for command queue

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
# IMPORTANT: Update this URL to your Intiface Engine's *Device Websocket Server* address and port
#            Ensure the Device Websocket Server is ENABLED in Intiface settings.
INTIFACE_WSDM_URL = "ws://127.0.0.1:54817"  # Example default WSDM port
LBIS_DEVICE_IP = "10.105.23.145"  # Your lBIS device's IP address (no http:// prefix needed here)
LBIS_DEVICE_PORT = 80
# LBIS_API_URL = f"http://{LBIS_DEVICE_IP}:{LBIS_DEVICE_PORT}/api/setPumpState" # Replaced by WS URL
LBIS_WS_URL = f"ws://{LBIS_DEVICE_IP}:{LBIS_DEVICE_PORT}/ws/pump" # Added WebSocket URL

# This identifier MUST match the 'identifier.identifier' in your buttplug-user-device-config.json
# AND the protocol name used in the 'protocols' section for the websocket communication.
WSDM_IDENTIFIER = "lovense"  # Changed to match the protocol we are emulating
# This should be a unique address for this specific shim instance, matching the UDCF
WSDM_ADDRESS = "6a797313-e431-4b9f-9fd0-3eef4c97df24"  # Use the same UUID as in the UDCF
WSDM_VERSION = 0  # Current WSDM protocol version

# --- Globals ---
message_id_counter = 1  # Counter for messages *sent* by the shim (like Ok)
pump_command_queue = Queue() # Queue for sending commands to lBIS device

async def send_intiface_message(ws, message_list):
    """Sends a properly formatted message list to the Intiface WSDM server."""
    global message_id_counter
    # Assign unique IDs to messages that require them (Ok, Error)
    for msg_dict in message_list:
        msg_type = list(msg_dict.keys())[0]
        if msg_type in ["Ok", "Error"]:  # Only add IDs to responses from the device/shim
            # Check if Id is already present (e.g. responding to a specific request ID)
            if "Id" not in msg_dict[msg_type]:
                msg_dict[msg_type]["Id"] = message_id_counter
                message_id_counter += 1

    msg_json = json.dumps(message_list)
    logging.debug(f"Sending to Intiface WSDM: {msg_json}")
    await ws.send_str(msg_json)

async def lbis_websocket_sender():
    """Connects to the lBIS device WebSocket and sends pump commands from the queue."""
    while True:
        logging.info(f"Attempting to connect to lBIS WebSocket at {LBIS_WS_URL}...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(LBIS_WS_URL, timeout=10) as ws:
                    logging.info("Connected to lBIS WebSocket.")
                    while True:
                        try:
                            # Wait for a command from the queue
                            speed = await pump_command_queue.get()
                            payload = json.dumps({"pump": speed})
                            logging.debug(f"Sending to lBIS WebSocket: {payload}")
                            await ws.send_str(payload)
                            pump_command_queue.task_done() # Mark task as complete
                            logging.info(f"Sent pump speed {speed:.2f} to lBIS via WebSocket.")

                            # Optional: Check for incoming messages if needed (e.g., confirmations, errors)
                            # try:
                            #     async for msg in ws:
                            #         if msg.type == aiohttp.WSMsgType.TEXT:
                            #             logging.debug(f"Received from lBIS WS: {msg.data}")
                            #         elif msg.type == aiohttp.WSMsgType.ERROR:
                            #             logging.error(f"lBIS WebSocket error: {ws.exception()}")
                            #             break # Break inner loop on error
                            #         elif msg.type == aiohttp.WSMsgType.CLOSED:
                            #             logging.info("lBIS WebSocket closed by server.")
                            #             break # Break inner loop on close
                            # except asyncio.TimeoutError:
                            #     continue # No message received, continue sending

                        except asyncio.CancelledError:
                            logging.info("lBIS WebSocket sender task cancelled.")
                            raise # Re-raise cancellation
                        except Exception as e:
                            logging.error(f"Error sending to lBIS WebSocket: {e}")
                            # Attempt to drain queue on error? Or just break and reconnect?
                            # For now, break to reconnect. Commands might be lost.
                            break # Break inner loop to reconnect

        except aiohttp.ClientConnectorError as e:
            logging.error(f"lBIS WebSocket connection failed: {e}")
        except asyncio.TimeoutError:
            logging.error(f"Timeout connecting to lBIS WebSocket at {LBIS_WS_URL}")
        except websockets.exceptions.InvalidHandshake as e:
            logging.error(f"lBIS WebSocket handshake failed: {e}")
        except Exception as e:
            logging.exception(f"Unexpected error in lBIS WebSocket sender: {e}")

        logging.info("Disconnected from lBIS WebSocket. Retrying in 5 seconds...")
        # Clear queue on disconnect? Or let commands build up?
        # Let them build up for now, Intiface might stop sending if device disconnects anyway.
        await asyncio.sleep(5)


async def handle_intiface_message(ws, msg_bytes): # Removed http_session
    """Processes a binary message received from the Intiface WSDM server, assuming Lovense plain text."""
    try:
        # Decode the binary message assuming UTF-8 (standard for Lovense text commands)
        msg = msg_bytes.decode('utf-8')
        logging.debug(f"Received and decoded from Intiface WSDM: {msg}")

        # Attempt to parse as Lovense plain text command
        if msg.endswith(';'):
            parts = msg[:-1].split(':')
            command = parts[0].strip()
            value_str = parts[1].strip() if len(parts) > 1 else None

            # Make command comparison case-insensitive
            command_lower = command.lower()

            if command_lower == "vibrate" and value_str is not None:
                try:
                    level = int(value_str)
                    if 0 <= level <= 20:
                        speed = float(level) / 20.0
                        logging.info(f"Parsed Lovense Vibrate: Level={level} -> Speed={speed:.2f}. Queueing command.")
                        # await set_lbis_pump_state(http_session, speed) # Replaced with queue put
                        await pump_command_queue.put(speed)
                        # Cannot send JSON Ok for plain text command
                    else:
                        logging.warning(f"Invalid Lovense Vibrate level: {level}")
                except ValueError:
                    logging.warning(f"Invalid Lovense Vibrate value: {value_str}")
            elif command_lower == "stop":
                logging.info("Parsed Lovense Stop command. Queueing pump speed 0.")
                # await set_lbis_pump_state(http_session, 0.0) # Replaced with queue put
                await pump_command_queue.put(0.0)
                # Cannot send JSON Ok for plain text command
            elif command_lower == "getbattery":
                logging.info("Parsed Lovense GetBattery command. (Ignoring, no response needed/possible here)")
                # Lovense devices typically respond automatically via BLE notifications,
                # which we can't easily replicate over WSDM without more complex handling.
            elif command_lower == "devicetype":
                # Acknowledge and ignore the DeviceType command
                logging.info("Parsed Lovense DeviceType command. (Ignoring)")
            else:
                logging.warning(f"Unhandled Lovense plain text command: {command}")
        else:
            # This case might occur if Intiface sends something unexpected
            logging.error(f"Decoded message not recognized as Lovense plain text: {msg}")

    except UnicodeDecodeError:
        logging.error(f"Failed to decode WSDM binary message as UTF-8: {msg_bytes!r}")
    except Exception as e:
        logging.exception(f"Error processing decoded Intiface WSDM message '{msg}': {e}")

async def intiface_client_loop():
    """Main loop to connect to Intiface WSDM and handle communication."""
    async with aiohttp.ClientSession() as session:
        while True:
            logging.info(f"Attempting to connect to Intiface WSDM at {INTIFACE_WSDM_URL}...")
            try:
                async with session.ws_connect(INTIFACE_WSDM_URL) as ws:
                    logging.info("Connected to Intiface WSDM.")
                    global message_id_counter
                    message_id_counter = 1  # Reset message counter on new connection

                    # 1. Send WSDM Handshake (using "lovense" identifier)
                    handshake_msg = {
                        "identifier": WSDM_IDENTIFIER,  # Now "lovense"
                        "address": WSDM_ADDRESS,
                        "version": WSDM_VERSION
                    }
                    handshake_json = json.dumps(handshake_msg)
                    logging.info(f"Sending WSDM Handshake: {handshake_json}")
                    await ws.send_str(handshake_json)

                    # 2. Start listening for device command messages
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            # Pass the raw bytes to the handler, no http_session needed
                            # await handle_intiface_message(ws, msg.data, http_session)
                            await handle_intiface_message(ws, msg.data)
                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            # Log if we unexpectedly get text
                            logging.warning(f"Received unexpected TEXT message from WSDM: {msg.data}")
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logging.error(f"WSDM WebSocket connection closed with exception {ws.exception()}")
                            break  # Exit inner loop to reconnect
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            logging.info("WSDM WebSocket connection closed by server.")
                            break  # Exit inner loop to reconnect

            except aiohttp.ClientConnectorError as e:
                logging.error(f"WSDM Connection failed: {e}")
            except websockets.exceptions.InvalidHandshake as e:  # Catch potential handshake rejection
                logging.error(f"WSDM Handshake failed: {e}. Check identifier and user device config.")
            except Exception as e:
                logging.exception(f"An unexpected error occurred in the WSDM client loop: {e}")

            logging.info("Disconnected from Intiface WSDM. Retrying in 5 seconds...")
            await asyncio.sleep(5)  # Wait before retrying connection

async def main():
    logging.info("Starting lBIS Intiface WSDM Shim...")
    # Create tasks for both loops
    intiface_task = asyncio.create_task(intiface_client_loop())
    lbis_sender_task = asyncio.create_task(lbis_websocket_sender())

    # Run them concurrently
    await asyncio.gather(
        intiface_task,
        lbis_sender_task
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shim stopped by user.")

