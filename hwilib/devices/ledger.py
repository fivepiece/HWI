# Ledger interaction script

from ..hwwclient import HardwareWalletClient
from btchip.btchip import *
from btchip.btchipUtils import *
import base64
import json
import struct
from .. import base58
from ..base58 import get_xpub_fingerprint_hex
from ..serializations import hash256, hash160, ser_uint256, PSBT, CTransaction, HexToBase64
import binascii
import logging

LEDGER_VENDOR_ID = 0x2c97
LEDGER_DEVICE_ID = 0x0001

# This class extends the HardwareWalletClient for Ledger Nano S specific things
class LedgerClient(HardwareWalletClient):

    def __init__(self, path, password=''):
        super(LedgerClient, self).__init__(path, password)
        device = hid.device()
        device.open_path(path.encode())
        device.set_nonblocking(True)

        self.dongle = HIDDongleHIDAPI(device, True, logging.getLogger().getEffectiveLevel() == logging.DEBUG)
        self.app = btchip(self.dongle)

    # Must return a dict with the xpub
    # Retrieves the public key at the specified BIP 32 derivation path
    def get_pubkey_at_path(self, path):
        path = path[2:]
        path = path.replace('h', '\'')
        path = path.replace('H', '\'')
        # This call returns raw uncompressed pubkey, chaincode
        pubkey = self.app.getWalletPublicKey(path)
        if path != "":
            parent_path = ""
            for ind in path.split("/")[:-1]:
                parent_path += ind+"/"
            parent_path = parent_path[:-1]

            # Get parent key fingerprint
            parent = self.app.getWalletPublicKey(parent_path)
            fpr = hash160(compress_public_key(parent["publicKey"]))[:4]

            # Compute child info
            childstr = path.split("/")[-1]
            hard = 0
            if childstr[-1] == "'" or childstr[-1] == "h" or childstr[-1] == "H":
                childstr = childstr[:-1]
                hard = 0x80000000
            child = struct.pack(">I", int(childstr)+hard)
        # Special case for m
        else:
            child = bytearray.fromhex("00000000")
            fpr = child

        chainCode = pubkey["chainCode"]
        publicKey = compress_public_key(pubkey["publicKey"])

        depth = len(path.split("/")) if len(path) > 0 else 0
        depth = struct.pack("B", depth)

        if self.is_testnet:
            version = bytearray.fromhex("043587CF")
        else:
            version = bytearray.fromhex("0488B21E")
        extkey = version+depth+fpr+child+chainCode+publicKey
        checksum = hash256(extkey)[:4]

        return {"xpub":base58.encode(extkey+checksum)}

    # Must return a hex string with the signed transaction
    # The tx must be in the combined unsigned transaction format
    # Current only supports segwit signing
    def sign_tx(self, tx):
        c_tx = CTransaction(tx.tx)
        tx_bytes = c_tx.serialize_with_witness()

        # Master key fingerprint
        master_fpr = hash160(compress_public_key(self.app.getWalletPublicKey('')["publicKey"]))[:4]
        # An entry per input, each with 0 to many keys to sign with
        all_signature_attempts = [[]]*len(c_tx.vin)

        # NOTE: We only support signing Segwit inputs, where we can skip over non-segwit
        # inputs, or non-segwit inputs, where *all* inputs are non-segwit. This is due
        # to Ledger's mutually exclusive signing steps for each type.
        segwit_inputs = []
        # Legacy style inputs
        legacy_inputs = []

        has_segwit = False
        has_legacy = False

        script_codes = [[]]*len(c_tx.vin)

        # Detect changepath, (p2sh-)p2(w)pkh only
        change_path = ''
        for txout, i_num in zip(c_tx.vout, range(len(c_tx.vout))):
            # Find which wallet key could be change based on hdsplit: m/.../1/k
            # Wallets shouldn't be sending to change address as user action
            # otherwise this will get confused
            for pubkey, path in tx.outputs[i_num].hd_keypaths.items():
                if struct.pack("<I", path[0]) == master_fpr and len(path) > 2 and path[-2] == 1:
                    # For possible matches, check if pubkey matches possible template
                    if hash160(pubkey) in txout.scriptPubKey or hash160(bytearray.fromhex("0014")+hash160(pubkey)) in txout.scriptPubKey:
                        change_path = ''
                        for index in path[1:]:
                            change_path += str(index)+"/"
                        change_path = change_path[:-1]


        for txin, psbt_in, i_num in zip(c_tx.vin, tx.inputs, range(len(c_tx.vin))):

            seq = format(txin.nSequence, 'x')
            seq = seq.zfill(8)
            seq = bytearray.fromhex(seq)
            seq.reverse()
            seq_hex = ''.join('{:02x}'.format(x) for x in seq)

            if psbt_in.non_witness_utxo:
                segwit_inputs.append({"value":txin.prevout.serialize()+struct.pack("<Q", psbt_in.non_witness_utxo.vout[txin.prevout.n].nValue), "witness":True, "sequence":seq_hex})
                # We only need legacy inputs in the case where all inputs are legacy, we check
                # later
                ledger_prevtx = bitcoinTransaction(psbt_in.non_witness_utxo.serialize())
                legacy_inputs.append(self.app.getTrustedInput(ledger_prevtx, txin.prevout.n))
                legacy_inputs[-1]["sequence"] = seq_hex
                has_legacy = True
            else:
                segwit_inputs.append({"value":txin.prevout.serialize()+struct.pack("<Q", psbt_in.witness_utxo.nValue), "witness":True, "sequence":seq_hex})
                has_segwit = True

            pubkeys = []
            signature_attempts = []

            scriptCode = b""
            witness_program = b""
            if psbt_in.witness_utxo is not None and psbt_in.witness_utxo.is_p2sh():
                redeemscript = psbt_in.redeem_script
                witness_program += redeemscript
            elif psbt_in.non_witness_utxo is not None and psbt_in.non_witness_utxo.vout[txin.prevout.n].is_p2sh():
                redeemscript = psbt_in.redeem_script
            elif psbt_in.witness_utxo is not None:
                witness_program += psbt_in.witness_utxo.scriptPubKey
            elif psbt_in.non_witness_utxo is not None:
                # No-op
                redeemscript = b""
                witness_program = b""
            else:
                raise Exception("PSBT is missing input utxo information, cannot sign")

            # Check if witness_program is script hash
            if len(witness_program) == 34 and witness_program[0] == 0x00 and witness_program[1] == 0x20:
                # look up witnessscript and set as scriptCode
                witnessscript = psbt_in.witness_script
                scriptCode += witnessscript
            elif len(witness_program) > 0:
                # p2wpkh
                scriptCode += b"\x76\xa9\x14"
                scriptCode += witness_program[2:]
                scriptCode += b"\x88\xac"
            elif len(witness_program) == 0:
                if len(redeemscript) > 0:
                    scriptCode = redeemscript
                else:
                    scriptCode = psbt_in.non_witness_utxo.vout[txin.prevout.n].scriptPubKey

            # Save scriptcode for later signing
            script_codes[i_num] = scriptCode

            # Find which pubkeys could sign this input (should be all?)
            for pubkey in psbt_in.hd_keypaths.keys():
                if hash160(pubkey) in scriptCode or pubkey in scriptCode:
                    pubkeys.append(pubkey)

            # Figure out which keys in inputs are from our wallet
            for pubkey in pubkeys:
                keypath = psbt_in.hd_keypaths[pubkey]
                if master_fpr == struct.pack("<I", keypath[0]):
                    # Add the keypath strings
                    keypath_str = ''
                    for index in keypath[1:]:
                        keypath_str += str(index) + "/"
                    keypath_str = keypath_str[:-1]
                    signature_attempts.append([keypath_str, pubkey])

            all_signature_attempts[i_num] = signature_attempts

        # Sign any segwit inputs
        if has_segwit:
            # Process them up front with all scriptcodes blank
            blank_script_code = bytearray()
            for i in range(len(segwit_inputs)):
                self.app.startUntrustedTransaction(i==0, i, segwit_inputs, blank_script_code, c_tx.nVersion)

            # Number of unused fields for Nano S, only changepath and transaction in bytes req
            outputData = self.app.finalizeInput(b"DUMMY", -1, -1, change_path, tx_bytes)

            # For each input we control do segwit signature
            for i in range(len(segwit_inputs)):
                # Don't try to sign legacy inputs
                if tx.inputs[i].non_witness_utxo is not None:
                    continue
                for signature_attempt in all_signature_attempts[i]:
                    self.app.startUntrustedTransaction(False, 0, [segwit_inputs[i]], script_codes[i], c_tx.nVersion)
                    tx.inputs[i].partial_sigs[signature_attempt[1]] = self.app.untrustedHashSign(signature_attempt[0], "", c_tx.nLockTime, 0x01)
        elif has_legacy:
            first_input = True
            # Legacy signing if all inputs are legacy
            for i in range(len(legacy_inputs)):
                for signature_attempt in all_signature_attempts[i]:
                    assert(tx.inputs[i].non_witness_utxo is not None)
                    self.app.startUntrustedTransaction(first_input, i, legacy_inputs, script_codes[i], c_tx.nVersion)
                    outputData = self.app.finalizeInput(b"DUMMY", -1, -1, change_path, tx_bytes)
                    tx.inputs[i].partial_sigs[signature_attempt[1]] = self.app.untrustedHashSign(signature_attempt[0], "", c_tx.nLockTime, 0x01)
                    first_input = False

        # Send PSBT back
        return {'psbt':tx.serialize()}

    # Must return a base64 encoded string with the signed message
    # The message can be any string
    def sign_message(self, message, keypath):
        message = bytearray(message, 'utf-8')
        keypath = keypath[2:]
        # First display on screen what address you're signing for
        self.app.getWalletPublicKey(keypath, True)
        self.app.signMessagePrepare(keypath, message)
        signature = self.app.signMessageSign()

        # Make signature into standard bitcoin format
        rLength = signature[3]
        r = signature[4 : 4 + rLength]
        sLength = signature[4 + rLength + 1]
        s = signature[4 + rLength + 2:]
        if rLength == 33:
            r = r[1:]
        if sLength == 33:
            s = s[1:]

        sig = bytearray(chr(27 + 4 + (signature[0] & 0x01)), 'utf8') + r + s

        return {"signature":base64.b64encode(sig).decode('utf-8')}

    def display_address(self, keypath, p2sh_p2wpkh, bech32):
        self.app.getWalletPublicKey(keypath[2:], True, (p2sh_p2wpkh or bech32), bech32)

    # Setup a new device
    def setup_device(self):
        raise NotImplementedError('The Ledger Nano S does not support software setup')

    # Wipe this device
    def wipe_device(self):
        raise NotImplementedError('The Ledger Nano S does not support wiping via software')

    # Close the device
    def close(self):
        self.dongle.close()

def enumerate(password=''):
    results = []
    for d in hid.enumerate(LEDGER_VENDOR_ID, LEDGER_DEVICE_ID):
        if ('interface_number' in d and  d['interface_number'] == 0 \
        or ('usage_page' in d and d['usage_page'] == 0xffa0)):
            d_data = {}

            path = d['path'].decode()
            d_data['type'] = 'ledger'
            d_data['path'] = path

            try:
                client = LedgerClient(path, password)
                master_xpub = client.get_pubkey_at_path('m/0h')['xpub']
                d_data['fingerprint'] = get_xpub_fingerprint_hex(master_xpub)
                client.close()
            except Exception as e:
                d_data['error'] = "Could not open client or get fingerprint information: " + str(e)

            results.append(d_data)
    return results
