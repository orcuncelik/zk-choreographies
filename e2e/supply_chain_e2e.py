"""Run the supply-chain choreography end to end.

Writes the proofs needed by the Hardhat gas tests to
`../solidity/test/supply_chain_proofs.json`.
"""

import requests
import base64
import time
import json
import os

BASE_BPMN = 'http://localhost:3000'
BASE_EXEC = 'http://localhost:8080'

# Shared message payload
PAYLOAD = base64.b32encode(b'payload').decode('utf-8')

# Proof output path, relative to `e2e/`
PROOFS_OUTPUT = '../solidity/test/supply_chain_proofs.json'


# Helpers

def print_token_counts(instance):
    tokens = instance['tokenCounts']
    active = [i for i, t in enumerate(tokens) if t > 0]
    print(f'    Token counts: {tokens}')
    print(f'    Active places: {active}')


def find_transition(transitions, name):
    """Find a transition by name."""
    for t in transitions:
        if t.get('name') == name:
            return t
    raise KeyError(f'Transition not found: {name!r}. '
                   f'Available: {[t.get("name") or t["id"] for t in transitions]}')


def find_event_transitions(transitions):
    """Return transitions with no initiator."""
    return [t for t in transitions if 'initiatingParticipant' not in t]


def three_phase_exchange(instance, transition, has_initiating_msg, has_responding_msg=False):
    """Run create, receive, and prove for one choreography task."""
    initiator_id = transition['initiatingParticipant']
    responder_id = transition['respondingParticipant']

    t_start = time.time()

    # Create
    create_cmd = {
        'instance': instance['id'],
        'transition': transition['id'],
    }
    if has_initiating_msg:
        create_cmd['bytesMessage'] = PAYLOAD

    resp = requests.post(f'{BASE_EXEC}/execution/createInitiatingMessage', json=create_cmd)
    if resp.status_code != 200:
        raise RuntimeError(f'createInitiatingMessage failed [{resp.status_code}]: {resp.text}')
    created = resp.json()
    init_msg = created.get('initiatingMessage')
    current_model = created['model']

    # Receive
    receive_cmd = {
        'model': current_model,
        'instance': instance,
        'transition': transition['id'],
        'identity': responder_id,
    }
    if init_msg is not None:
        receive_cmd['initiatingMessage'] = init_msg
    if has_responding_msg:
        receive_cmd['bytesMessage'] = PAYLOAD

    resp = requests.post(f'{BASE_EXEC}/execution/receiveInitiatingMessage', json=receive_cmd)
    if resp.status_code != 200:
        raise RuntimeError(f'receiveInitiatingMessage failed [{resp.status_code}]: {resp.text}')
    received = resp.json()
    next_instance = received['nextInstance']
    responding_sig = received['respondingParticipantSignature']
    responding_msg = received.get('respondingMessage')

    # Prove
    prove_cmd = {
        'currentInstance': instance['id'],
        'transition': transition['id'],
        'identity': initiator_id,
        'nextInstance': next_instance,
        'respondingParticipantSignature': responding_sig,
    }
    if init_msg is not None:
        prove_cmd['initiatingMessage'] = init_msg['id']
    if responding_msg is not None:
        prove_cmd['respondingMessage'] = responding_msg

    resp = requests.post(f'{BASE_EXEC}/execution/proveMessageExchange', json=prove_cmd)
    if resp.status_code != 200:
        raise RuntimeError(f'proveMessageExchange failed [{resp.status_code}]: {resp.text}')
    result = resp.json()

    elapsed = time.time() - t_start
    timing = result['proof'].get('timing', {})
    witness_ms = timing.get('witnessMs', 0.0)
    proof_ms   = timing.get('proofMs', 0.0)
    print(f'    Total: {elapsed:.2f}s  |  witness: {witness_ms:.2f}ms  proof: {proof_ms:.0f}ms')
    return result['instance'], result['proof'], elapsed, witness_ms, proof_ms


def simple_transition(instance, transition):
    """Run a start or end event."""
    t_start = time.time()
    resp = requests.post(f'{BASE_EXEC}/execution/executeTransition', json={
        'instance': instance['id'],
        'transition': transition['id'],
        'identity': 0,
    })
    if resp.status_code != 200:
        raise RuntimeError(f'executeTransition failed [{resp.status_code}]: {resp.text}')
    result = resp.json()
    elapsed = time.time() - t_start
    timing = result['proof'].get('timing', {})
    witness_ms = timing.get('witnessMs', 0.0)
    proof_ms   = timing.get('proofMs', 0.0)
    print(f'    Total: {elapsed:.2f}s  |  witness: {witness_ms:.2f}ms  proof: {proof_ms:.0f}ms')
    return result['instance'], result['proof'], elapsed, witness_ms, proof_ms


# Main

def main():
    wall_start = time.time()
    proofs_for_hardhat = []      # instantiation, first transition, termination
    timing_log = {}              # step -> elapsed seconds
    witness_log = {}             # step -> witness ms
    proof_ms_log = {}            # step -> proof ms

    print('=' * 70)
    print('SUPPLY CHAIN CHOREOGRAPHY - FULL END-TO-END EXECUTION')
    print('=' * 70)
    print()

    # Step 1: submit BPMN
    print('[Step 1] Submitting supply-chain.bpmn to BPMN service...')
    bpmn_path = os.path.join(os.path.dirname(__file__), '../bpmn/supply-chain.bpmn')
    with open(bpmn_path, 'r') as f:
        bpmn_xml = f.read()

    resp = requests.post(f'{BASE_BPMN}/choreographies', json={'xmlString': bpmn_xml})
    if resp.status_code not in (200, 201):
        raise RuntimeError(f'BPMN submission failed [{resp.status_code}]: {resp.text}')
    model_id = resp.json()['id']
    print(f'  Model ID: {model_id}')

    # Step 2: load model
    print('\n[Step 2] Retrieving model from execution service...')
    model = requests.get(f'{BASE_EXEC}/models/{model_id}').json()
    trans = model['transitions']
    n_participants = model['participantCount']
    print(f'  Place count:       {model["placeCount"]}')
    print(f'  Participant count: {n_participants}')
    print(f'  Message count:     {model["messageCount"]}')
    print(f'  Start places:      {model["startPlaces"]}')
    print(f'  End places:        {model["endPlaces"]}')
    print(f'  Transitions ({len(trans)}):')
    for i, t in enumerate(trans):
        parts = []
        if 'initiatingParticipant' in t:
            parts.append(f'initiator={t["initiatingParticipant"]}')
        if 'respondingParticipant' in t:
            parts.append(f'responder={t["respondingParticipant"]}')
        if 'initiatingMessage' in t:
            parts.append(f'initMsg={t["initiatingMessage"]}')
        name = t['name'] if t.get('name') else t['id']
        print(f'    [{i}] {name}: in={t["incomingPlaces"]} out={t["outgoingPlaces"]} {" ".join(parts)}')

    # Step 3: get public keys
    print('\n[Step 3] Getting public keys...')
    all_keys = requests.get(f'{BASE_EXEC}/publicKeys').json()
    if len(all_keys) < n_participants:
        raise RuntimeError(
            f'Need at least {n_participants} public keys, got {len(all_keys)}. '
            f'Set IdentityCount >= {n_participants} in execution-service/domain/signature.go'
        )
    public_keys = all_keys[:n_participants]
    print(f'  Using {n_participants} keys (one per participant)')

    # Step 4: instantiate model
    print('\n[Step 4] Instantiating model...')
    t_start = time.time()
    resp = requests.post(f'{BASE_EXEC}/execution/instantiateModel', json={
        'model': model_id,
        'publicKeys': public_keys,
        'identity': 0,
    })
    if resp.status_code != 200:
        raise RuntimeError(f'Instantiation failed [{resp.status_code}]: {resp.text}')
    inst_event = resp.json()
    instance = inst_event['instance']
    inst_proof = inst_event['proof']
    elapsed_inst = time.time() - t_start
    inst_timing = inst_proof.get('timing', {})
    proofs_for_hardhat.append(inst_proof)
    timing_log['instantiation'] = elapsed_inst
    witness_log['instantiation'] = inst_timing.get('witnessMs', 0.0)
    proof_ms_log['instantiation'] = inst_timing.get('proofMs', 0.0)
    print(f'  Instance: {instance["id"]}')
    print(f'  Proof generated in {elapsed_inst:.2f}s  |  witness: {witness_log["instantiation"]:.2f}ms  proof: {proof_ms_log["instantiation"]:.0f}ms')
    print_token_counts(instance)

    # Step 5: look up transitions
    # Event transitions
    event_trans = find_event_transitions(trans)
    start_event = next(t for t in event_trans if model['startPlaces'][0] in t['incomingPlaces'])
    end_event   = next(t for t in event_trans if model['endPlaces'][0] in t['outgoingPlaces'])

    t_order_goods          = find_transition(trans, 'Order goods')
    t_place_order          = find_transition(trans, 'Place order for supplies')
    t_forward_order        = find_transition(trans, 'Forward order for supplies')
    t_place_transport      = find_transition(trans, 'Place order for transport')
    t_exchange_details     = find_transition(trans, 'Exchange details')
    t_send_waybill         = find_transition(trans, 'Send waybill')
    t_deliver_supplies     = find_transition(trans, 'Deliver supplies')
    t_report_production    = find_transition(trans, 'Report start of production')
    t_deliver_goods        = find_transition(trans, 'Deliver goods')

    first_transition_proof_saved = False

    def record_proof(name, proof, elapsed, witness_ms=0.0, proof_ms=0.0):
        nonlocal first_transition_proof_saved
        timing_log[name] = elapsed
        witness_log[name] = witness_ms
        proof_ms_log[name] = proof_ms
        if not first_transition_proof_saved:
            proofs_for_hardhat.append(proof)
            first_transition_proof_saved = True

    # Step 5a: start event
    print('\n[Step 5a] Start Event...')
    instance, proof, elapsed, wms, pms = simple_transition(instance, start_event)
    record_proof('start_event', proof, elapsed, wms, pms)
    print_token_counts(instance)

    # "Deliver goods" uses an unnamed message, so has_initiating_msg stays False there.

    # Step 5b: Order Goods
    print('\n[Step 5b] Order Goods (Bulk Buyer → Manufacturer)...')
    instance, proof, elapsed, wms, pms = three_phase_exchange(
        instance, t_order_goods, has_initiating_msg=True
    )
    record_proof('order_goods', proof, elapsed, wms, pms)
    print_token_counts(instance)

    # Step 5c: Place Order for Supplies
    print('\n[Step 5c] Place Order for Supplies (Manufacturer → Middleman) [parallel split]...')
    instance, proof, elapsed, wms, pms = three_phase_exchange(
        instance, t_place_order, has_initiating_msg=True
    )
    record_proof('place_order_supplies', proof, elapsed, wms, pms)
    print_token_counts(instance)

    # Step 5d: Forward Order for Supplies
    print('\n[Step 5d] Forward Order for Supplies (Middleman → Supplier) [parallel branch 1]...')
    instance, proof, elapsed, wms, pms = three_phase_exchange(
        instance, t_forward_order, has_initiating_msg=True
    )
    record_proof('forward_order_supplies', proof, elapsed, wms, pms)
    print_token_counts(instance)

    # Step 5e: Place Order for Transport
    print('\n[Step 5e] Place Order for Transport (Middleman → Special Carrier) [parallel branch 2]...')
    instance, proof, elapsed, wms, pms = three_phase_exchange(
        instance, t_place_transport, has_initiating_msg=True
    )
    record_proof('place_order_transport', proof, elapsed, wms, pms)
    print_token_counts(instance)

    # Step 5f: Exchange Details
    print('\n[Step 5f] Exchange Details (Special Carrier ↔ Supplier) [parallel join]...')
    instance, proof, elapsed, wms, pms = three_phase_exchange(
        instance, t_exchange_details, has_initiating_msg=True, has_responding_msg=True
    )
    record_proof('exchange_details', proof, elapsed, wms, pms)
    print_token_counts(instance)

    # Step 5g: Send Waybill
    print('\n[Step 5g] Send Waybill (Supplier → Special Carrier)...')
    instance, proof, elapsed, wms, pms = three_phase_exchange(
        instance, t_send_waybill, has_initiating_msg=True
    )
    record_proof('send_waybill', proof, elapsed, wms, pms)
    print_token_counts(instance)

    # Step 5h: Deliver Supplies
    print('\n[Step 5h] Deliver Supplies (Special Carrier → Manufacturer)...')
    instance, proof, elapsed, wms, pms = three_phase_exchange(
        instance, t_deliver_supplies, has_initiating_msg=True
    )
    record_proof('deliver_supplies', proof, elapsed, wms, pms)
    print_token_counts(instance)

    # Step 5i: Report Start of Production
    print('\n[Step 5i] Report Start of Production (Manufacturer → Bulk Buyer)...')
    instance, proof, elapsed, wms, pms = three_phase_exchange(
        instance, t_report_production, has_initiating_msg=True
    )
    record_proof('report_production', proof, elapsed, wms, pms)
    print_token_counts(instance)

    # Step 5j: Deliver Goods
    # This task uses an unnamed message, so has_initiating_msg is False.
    print('\n[Step 5j] Deliver Goods (Manufacturer → Bulk Buyer)...')
    instance, proof, elapsed, wms, pms = three_phase_exchange(
        instance, t_deliver_goods, has_initiating_msg=False
    )
    record_proof('deliver_goods', proof, elapsed, wms, pms)
    print_token_counts(instance)

    # Step 5k: end event
    print('\n[Step 5k] End Event...')
    instance, proof, elapsed, wms, pms = simple_transition(instance, end_event)
    record_proof('end_event', proof, elapsed, wms, pms)
    print_token_counts(instance)

    # Step 6: prove termination
    print('\n[Step 6] Proving termination...')
    t_start = time.time()
    resp = requests.post(f'{BASE_EXEC}/execution/proveTermination', json={
        'instance': instance['id'],
        'identity': 0,
    })
    if resp.status_code != 200:
        raise RuntimeError(f'proveTermination failed [{resp.status_code}]: {resp.text}')
    term_proof = resp.json()['proof']
    elapsed_term = time.time() - t_start
    term_timing = term_proof.get('timing', {})
    proofs_for_hardhat.append(term_proof)
    timing_log['termination'] = elapsed_term
    witness_log['termination'] = term_timing.get('witnessMs', 0.0)
    proof_ms_log['termination'] = term_timing.get('proofMs', 0.0)
    print(f'  Proof generated in {elapsed_term:.2f}s  |  witness: {witness_log["termination"]:.2f}ms  proof: {proof_ms_log["termination"]:.0f}ms')

    # Save proofs for the Hardhat tests
    out_path = os.path.join(os.path.dirname(__file__), PROOFS_OUTPUT)
    with open(out_path, 'w') as f:
        json.dump(proofs_for_hardhat, f, indent=2)
    print(f'\n  Proofs saved to {PROOFS_OUTPUT} ({len(proofs_for_hardhat)} entries)')

    # Summary
    token_counts = instance['tokenCounts']
    end_places = set(model['endPlaces'])
    all_at_end = all(
        (token_counts[i] == 1 if i in end_places else token_counts[i] == 0)
        for i in range(len(token_counts))
    )
    non_end_zeros = all(token_counts[i] == 0 for i in range(len(token_counts)) if i not in end_places)
    wall_elapsed = time.time() - wall_start
    n_choreography_tasks = len(trans) - 2  # start and end are handled separately

    print()
    print('=' * 70)
    print('EXECUTION SUMMARY')
    print('=' * 70)
    print(f'  Model:             Supply Chain Choreography')
    print(f'  Participants:      {n_participants}')
    print(f'  Places:            {model["placeCount"]}')
    print(f'  Messages:          {model["messageCount"]}')
    print(f'  Transitions:       {len(trans)} total ({n_choreography_tasks} choreography tasks + 2 events)')
    print()

    # Circuit setup summary
    print('  Circuit Initialization (one-time, printed at service startup):')
    print(f'    {"Circuit":<16}  {"Compile Time":>14}  {"Setup Time":>14}')
    print(f'    {"-"*16}  {"-"*14}  {"-"*14}')
    for circ in ('instantiation', 'transition', 'termination'):
        print(f'    {circ:<16}  {"(see svc log)":>14}  {"(see svc log)":>14}')
    print()

    # Per-proof timing
    print('  Per-Proof Timing:')
    print(f'    {"Step":<28}  {"Compile":>10}  {"Setup":>10}  {"Witness(ms)":>12}  {"Proof(ms)":>12}  {"Total(s)":>9}')
    print(f'    {"-"*28}  {"-"*10}  {"-"*10}  {"-"*12}  {"-"*12}  {"-"*9}')
    for name, elapsed in timing_log.items():
        wms = witness_log.get(name, 0.0)
        pms = proof_ms_log.get(name, 0.0)
        print(f'    {name:<28}  {"N/A":>10}  {"N/A":>10}  {wms:>12.2f}  {pms:>12.2f}  {elapsed:>9.2f}')

    proof_names = [k for k in timing_log if k not in ('start_event', 'end_event')]
    if proof_names:
        avg_wms     = sum(witness_log.get(k, 0) for k in proof_names) / len(proof_names)
        avg_pms     = sum(proof_ms_log.get(k, 0) for k in proof_names) / len(proof_names)
        avg_elapsed = sum(timing_log.get(k, 0) for k in proof_names) / len(proof_names)
        print(f'    {"-"*28}  {"-"*10}  {"-"*10}  {"-"*12}  {"-"*12}  {"-"*9}')
        print(f'    {"avg (excl. events)":<28}  {"":>10}  {"":>10}  {avg_wms:>12.2f}  {avg_pms:>12.2f}  {avg_elapsed:>9.2f}')
    print()

    print(f'  Final instance:')
    print(f'    Token counts: {token_counts}')
    print(f'    All tokens at end places: {all_at_end}')
    print(f'    All non-end tokens zero:  {non_end_zeros}')
    print()
    print(f'  Total wall time: {wall_elapsed:.1f}s')
    print()
    print('  Choreography execution completed successfully!' if all_at_end and non_end_zeros
          else '  WARNING: Final state is not as expected!')
    print('=' * 70)

    return timing_log


if __name__ == '__main__':
    main()
