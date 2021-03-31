#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright (c) 2021 tecnovert
# Distributed under the MIT software license, see the accompanying
# file LICENSE.txt or http://www.opensource.org/licenses/mit-license.php.

"""

Verifies
 - Claimed amounts and blinding factors match amount commitments.
 - Claimed anon input keyimages match those on spending txs.
   - If anon spendkeys are provided.
 - Check claimed anon index is possible.
 - Check claimed anon indices are not reused.
   - Write to file to cache across all runs
 - Keyimages are not reused.

~/tmp/particl-0.19.2.5/bin/particl-qt -txindex=1 -server -printtoconsole=0 -nodebuglogfile
./particl-cli -rpcwallet=wallet.dat debugwallet "{\"trace_frozen_outputs\":true}"  > ~/trace_wallets.txt
$ python trace_frozen.py ~/.particl ~/trace_wallets.txt

"""

import os
import sys
import json
import time
from util import callrpc, make_int, format8, b58decode
from ecc_util import hashToCurve, pointToCPK, G, b2i, b2h


spent_ai_file = os.path.expanduser(os.getenv('SPENT_ANON_INPUTS_FILE', '~/trace_frozen_spent_inputs.json'))


def fromWIF(x):
    return b58decode(x)[1:-5]


def main():
    use_anon_spend_keys = False
    particl_data_dir = os.path.expanduser(sys.argv[1])
    input_file = os.path.expanduser(sys.argv[2])

    if len(sys.argv) > 3:
        use_anon_spend_keys = True if sys.argv[3].lower() == 'true' else False

    chain = 'mainnet'

    authcookiepath = os.path.join(particl_data_dir, '' if chain == 'mainnet' else chain, '.cookie')
    for i in range(10):
        if not os.path.exists(authcookiepath):
            time.sleep(0.5)
    with open(authcookiepath) as fp:
        rpc_auth = fp.read()

    rpc_port = 51735 if chain == 'mainnet' else 51935

    r = callrpc(rpc_port, rpc_auth, 'getnetworkinfo')
    print('Core version', r['version'])
    print('Use anon spend keys', use_anon_spend_keys)
    print('Spent anon indices path', spent_ai_file)

    spent_anon_inputs = {}
    if os.path.exists(spent_ai_file):
        with open(spent_ai_file) as fp:
            spent_anon_inputs = json.load(fp)
    print('Spent anon indices', len(spent_anon_inputs))

    with open(input_file) as fp:
        input_json = json.load(fp)

    used_keyimages = set()

    def trace_tx_inputs(itx, spending_txid, spending_tx, issues):
        txid = itx['txid']
        tx = callrpc(rpc_port, rpc_auth, 'getrawtransaction', [txid, True])

        if 'ct_fee' in tx['vout'][0]:
            ct_fee = tx['vout'][0]['ct_fee']
        else:
            ct_fee = 0

        total_in = 0
        total_out = make_int(ct_fee)
        spent_out = 0  # pass up to verify spending_txid

        # Verify the claimed output amounts
        for txo in tx['vout']:
            txo_type = txo['type']
            if txo_type in ['anon', 'blind']:
                found_vout = False
                for txo_verify in itx['outputs']:
                    if txo['n'] != txo_verify['n']:
                        continue
                    rv = callrpc(rpc_port, rpc_auth, 'verifycommitment', [txo['valueCommitment'], txo_verify['blind'], format8(txo_verify['value'])])
                    assert(rv['result'] is True)
                    total_out += txo_verify['value']
                    found_vout = True

                    if spending_txid is not None and 'spent_by' in txo_verify and spending_txid == txo_verify['spent_by']:
                        spent_out += txo_verify['value']
                        # Verify anon_index is possible
                        if txo_type == 'anon':
                            anon_index = txo_verify['anon_index']
                            found_input = False
                            for txin in spending_tx['vin']:
                                for i in range(1000):
                                    row = 'ring_row_{}'.format(i)
                                    if row not in txin:
                                        break
                                    ais = txin[row].split(',')
                                    for ai in ais:
                                        if anon_index == int(ai.strip()):
                                            found_input = True
                                            break
                            assert(found_input)

                            if str(anon_index) in spent_anon_inputs:
                                assert(spent_anon_inputs[str(anon_index)] == spending_txid)
                            else:
                                spent_anon_inputs[str(anon_index)] = spending_txid

                        if txo_type == 'anon' and use_anon_spend_keys:
                            anon_sk = b2i(fromWIF(txo_verify['anon_spend_key']))
                            anon_pk = G * anon_sk
                            H = hashToCurve(pointToCPK(anon_pk))

                            expect_keyimage = H * anon_sk
                            expect_keyimage_b = pointToCPK(expect_keyimage)
                            expect_keyimage_str = b2h(expect_keyimage_b)
                            # Match keyimage to tx vin
                            found_ki = False
                            for txin in spending_tx['vin']:
                                for sd in txin['scriptdata']:
                                    if expect_keyimage_str in sd:
                                        found_ki = True
                                        break
                            assert(found_ki)

                            assert(expect_keyimage_b not in used_keyimages)
                            used_keyimages.add(expect_keyimage_b)
                    break
                if found_vout is False:
                    warning = 'Warning: Missing output {} for tx {}.'.format(txo['n'], txid)
                    print(warning)
                    issues.append(warning)
            elif txo_type == 'standard':
                total_out += txo['valueSat']

        if 'inputs' in itx:
            for txi_verify in itx['inputs']:
                total_in += trace_tx_inputs(txi_verify, txid, tx, issues)
        else:
            for txin in tx['vin']:
                if 'type' in txin and txin['type'] != 'standard':
                    warning = 'Warning: Missing blinded inputs for tx {}.'.format(txid)
                    print(warning)
                    issues.append(warning)
                else:
                    prev_tx = callrpc(rpc_port, rpc_auth, 'getrawtransaction', [txin['txid'], True])
                    prevout = prev_tx['vout'][txin['vout']]
                    assert(prevout['type'] == 'standard')
                    total_in += prevout['valueSat']

        print('txid', txid)
        if spending_txid is not None:
            print('input for ', spending_txid)
        print('total_in', total_in)
        print('total_out', total_out)

        if total_out > total_in:
            warning = 'Warning: Mismatched value for tx: out > in {}.'.format(txid)
            print(warning)
            issues.append(warning)
        elif total_in != total_out:
            warning = 'Warning: Mismatched value for tx: in != out {}.'.format(txid)
            print(warning)
            issues.append(warning)

        return spent_out

    txids_likely_valid = []
    txids_check_further = []
    for itx in input_json['transactions']:
        print('')
        issues = []
        trace_tx_inputs(itx, None, None, issues)
        if len(issues) == 0:
            txids_likely_valid.append(itx['txid'])
        else:
            txids_check_further.append(itx['txid'])
    print('')
    print('Likely valid txids:')
    print('\n'.join(txids_likely_valid))

    print('Unproven txids:')
    print('\n'.join(txids_check_further))

    with open(spent_ai_file, 'w') as fp:
        json.dump(spent_anon_inputs, fp, indent=4)

    print('Done.')


if __name__ == '__main__':
    main()