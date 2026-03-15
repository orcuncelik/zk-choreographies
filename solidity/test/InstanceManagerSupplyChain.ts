/**
 * L1 gas test for the supply-chain choreography.
 * Uses `test/supply_chain_proofs.json`.
 * Run with `REPORT_GAS=true npx hardhat test test/InstanceManagerSupplyChain.ts`.
 */

import { ignition, ethers } from 'hardhat';
import * as fs from 'fs';
import { expect } from 'chai';
import { buildModule } from '@nomicfoundation/ignition-core';

const abiBytes = (hexData: string) => (hexData.length - 2) / 2;

// Ignition modules

const SupplyChainMockModule = buildModule('SupplyChainMockModule', (m) => {
  const instantiationVerifier = m.contract('InstantiationVerifierMock');
  const transitionVerifier    = m.contract('TransitionVerifier');
  const terminationVerifier   = m.contract('TerminationVerifier');
  const instanceManager       = m.contract('InstanceManager',
    [instantiationVerifier, transitionVerifier, terminationVerifier]);
  return { instanceManager };
});

// Tests

describe('InstanceManager (Supply Chain — L1)', function () {
  let proofs: any[];

  const gasLog:          Record<string, bigint> = {};
  const gasPriceLog:     Record<string, bigint> = {};
  const feeLog:          Record<string, bigint> = {};
  const deployGas:       Record<string, bigint> = {};
  const deployFee:       Record<string, bigint> = {};
  const calldataLog:     Record<string, number> = {};
  const bytecodeSizeLog: Record<string, number> = {};
  const inputCountLog:   Record<string, number> = {};
  let   proofSizeBytes = 0;
  let   chainId: bigint = 0n;

  before(function () {
    const buf = fs.readFileSync('test/supply_chain_proofs.json');
    proofs = JSON.parse(buf.toString());
    if (proofs.length < 3) {
      throw new Error(
        'supply_chain_proofs.json must have at least 3 entries. ' +
        'Run: cd e2e && python3 supply_chain_e2e.py'
      );
    }
    // Groth16 BN254 proofs are always 8 field elements.
    proofSizeBytes = proofs[0].value.length * 32;
    // Public input counts per operation.
    inputCountLog['instantiate'] = proofs[0].input.length;
    inputCountLog['transition']  = proofs[1].input.length;
    inputCountLog['terminate']   = proofs[proofs.length - 1].input.length;
  });

  it('instantiation (real verifier)', async function () {
    // Deploy manually so we can capture deployment receipts.
    const ivFactory = await ethers.getContractFactory('InstantiationVerifier');
    const instantiationVerifier = await ivFactory.deploy();
    await instantiationVerifier.waitForDeployment();

    const tvFactory = await ethers.getContractFactory('TransitionVerifier');
    const transitionVerifier = await tvFactory.deploy();
    await transitionVerifier.waitForDeployment();

    const rvFactory = await ethers.getContractFactory('TerminationVerifier');
    const terminationVerifier = await rvFactory.deploy();
    await terminationVerifier.waitForDeployment();

    const imFactory = await ethers.getContractFactory('InstanceManager');
    const instanceManager = await imFactory.deploy(
      instantiationVerifier.target, transitionVerifier.target, terminationVerifier.target);
    await instanceManager.waitForDeployment();

    // Chain ID
    const network = await ethers.provider.getNetwork();
    chainId = network.chainId;

    // Deployed bytecode sizes
    const [ivCode, tvCode, rvCode, imCode] = await Promise.all([
      ethers.provider.getCode(instantiationVerifier.target),
      ethers.provider.getCode(transitionVerifier.target),
      ethers.provider.getCode(terminationVerifier.target),
      ethers.provider.getCode(instanceManager.target),
    ]);
    bytecodeSizeLog['InstantiationVerifier'] = (ivCode.length - 2) / 2;
    bytecodeSizeLog['TransitionVerifier']    = (tvCode.length - 2) / 2;
    bytecodeSizeLog['TerminationVerifier']   = (rvCode.length - 2) / 2;
    bytecodeSizeLog['InstanceManager']       = (imCode.length - 2) / 2;

    const [ivR, tvR, rvR, imR] = await Promise.all([
      instantiationVerifier.deploymentTransaction()!.wait(),
      transitionVerifier.deploymentTransaction()!.wait(),
      terminationVerifier.deploymentTransaction()!.wait(),
      instanceManager.deploymentTransaction()!.wait(),
    ]);
    if (ivR) {
      deployGas['InstantiationVerifier'] = ivR.gasUsed;
      deployFee['InstantiationVerifier'] = ivR.gasUsed * BigInt(ivR.gasPrice ?? 0);
    }
    if (tvR) {
      deployGas['TransitionVerifier'] = tvR.gasUsed;
      deployFee['TransitionVerifier'] = tvR.gasUsed * BigInt(tvR.gasPrice ?? 0);
    }
    if (rvR) {
      deployGas['TerminationVerifier'] = rvR.gasUsed;
      deployFee['TerminationVerifier'] = rvR.gasUsed * BigInt(rvR.gasPrice ?? 0);
    }
    if (imR) {
      deployGas['InstanceManager'] = imR.gasUsed;
      deployFee['InstanceManager'] = imR.gasUsed * BigInt(imR.gasPrice ?? 0);
    }

    const p  = proofs[0];
    const tx = await instanceManager.instantiate(p.value, p.input[0]);
    const r  = await tx.wait();
    gasLog['instantiate (real)']      = r!.gasUsed;
    gasPriceLog['instantiate (real)'] = BigInt(r!.gasPrice ?? 0);
    feeLog['instantiate (real)']      = r!.gasUsed * BigInt(r!.gasPrice ?? 0);

    // ABI payload size only.
    calldataLog['instantiate'] =
      abiBytes(instanceManager.interface.encodeFunctionData('instantiate', [p.value, p.input[0]]));

    expect(await instanceManager.instances(p.input[0])).to.be.true;

    const fee = feeLog['instantiate (real)'];
    console.log(`  instantiate (real): ${r!.gasUsed.toLocaleString()} gas  (gasPrice: ${r!.gasPrice} wei, fee: ${fee} wei)`);
  });

  it('transition', async function () {
    const { instanceManager } = await ignition.deploy(SupplyChainMockModule);

    const p = proofs[1];
    await instanceManager.instantiate(p.value, p.input[0]); // mock — skips proof
    const tx = await instanceManager.transition(p.value, p.input[0], p.input[1]);
    const r  = await tx.wait();
    gasLog['transition']      = r!.gasUsed;
    gasPriceLog['transition'] = BigInt(r!.gasPrice ?? 0);
    feeLog['transition']      = r!.gasUsed * BigInt(r!.gasPrice ?? 0);

    calldataLog['transition'] =
      abiBytes(instanceManager.interface.encodeFunctionData('transition', [p.value, p.input[0], p.input[1]]));

    expect(await instanceManager.instances(p.input[0])).to.be.false;
    expect(await instanceManager.instances(p.input[1])).to.be.true;
    const fee = feeLog['transition'];
    console.log(`  transition:         ${r!.gasUsed.toLocaleString()} gas  (gasPrice: ${r!.gasPrice} wei, fee: ${fee} wei)`);
  });

  it('termination', async function () {
    const { instanceManager } = await ignition.deploy(SupplyChainMockModule);

    const p = proofs[proofs.length - 1];
    await instanceManager.instantiate(p.value, p.input[0]); // mock — skips proof
    const tx = await instanceManager.terminate(p.value, p.input[0]);
    const r  = await tx.wait();
    gasLog['terminate']      = r!.gasUsed;
    gasPriceLog['terminate'] = BigInt(r!.gasPrice ?? 0);
    feeLog['terminate']      = r!.gasUsed * BigInt(r!.gasPrice ?? 0);

    calldataLog['terminate'] =
      abiBytes(instanceManager.interface.encodeFunctionData('terminate', [p.value, p.input[0]]));

    expect(await instanceManager.instances(p.input[0])).to.be.false;
    const fee = feeLog['terminate'];
    console.log(`  terminate:          ${r!.gasUsed.toLocaleString()} gas  (gasPrice: ${r!.gasPrice} wei, fee: ${fee} wei)`);
  });

  after(function () {
    const N_TRANSITIONS = 9; // supply chain has 9 choreography tasks

    console.log('\n┌──────────────────────────┬──────────────┬──────────────────────┐');
    console.log('│  Supply Chain L1 Gas Summary                                   │');
    console.log('├──────────────────────────┼──────────────┼──────────────────────┤');
    console.log('│  Method                  │  gasUsed     │  fee paid (wei)      │');
    console.log('├──────────────────────────┼──────────────┼──────────────────────┤');
    for (const [k, v] of Object.entries(gasLog)) {
      const fee = feeLog[k] ?? 0n;
      console.log(`│  ${k.padEnd(24)} │  ${v.toString().padStart(12)} │  ${fee.toString().padStart(20)} │`);
    }
    console.log('└──────────────────────────┴──────────────┴──────────────────────┘');

    const inst  = gasLog['instantiate (real)'] ?? 0n;
    const trans = gasLog['transition']          ?? 0n;
    const term  = gasLog['terminate']            ?? 0n;
    const execTotal = inst + trans * BigInt(N_TRANSITIONS) + term;
    const execFeeTotal = (feeLog['instantiate (real)'] ?? 0n)
      + (feeLog['transition'] ?? 0n) * BigInt(N_TRANSITIONS)
      + (feeLog['terminate'] ?? 0n);
    const deployFeeTotal = Object.values(deployFee).reduce((a, b) => a + b, 0n);
    const grandFeeTotal = deployFeeTotal + execFeeTotal;

    console.log(`\n  Full Supply Chain estimated on-chain cost:`);
    console.log(`    instantiate (×1):             ${inst.toLocaleString()}`);
    console.log(`    transition  (×${N_TRANSITIONS}):            ${(trans * BigInt(N_TRANSITIONS)).toLocaleString()}`);
    console.log(`    terminate   (×1):             ${term.toLocaleString()}`);
    console.log(`    ─────────────────────────────────────────`);
    console.log(`    TOTAL:                        ${execTotal.toLocaleString()} gas`);
    console.log(`    (Deployment gas: see gas-reporter Deployments table above)`);

    // Summary lines for run_choreography.py
    console.log(`\n  l1-chain-id: ${chainId}`);
    console.log(`  l1-transition-count: ${N_TRANSITIONS}`);
    console.log(`  l1-exec-total: ${execTotal.toLocaleString()} gas`);
    console.log(`  l1-proof-size-bytes: ${proofSizeBytes}`);
    console.log(`  l1-exec-total-fee: ${execFeeTotal}`);
    if (Object.keys(deployFee).length > 0) {
      console.log(`  l1-deploy-total-fee: ${deployFeeTotal}`);
      console.log(`  l1-grand-total-fee: ${grandFeeTotal}`);
    }

    console.log(`  l1-instantiate-gasprice: ${gasPriceLog['instantiate (real)'] ?? 0n}`);
    console.log(`  l1-instantiate-fee: ${feeLog['instantiate (real)'] ?? 0n}`);
    console.log(`  l1-instantiate-calldata-bytes: ${calldataLog['instantiate'] ?? 0}`);
    console.log(`  l1-instantiate-input-count: ${inputCountLog['instantiate'] ?? 0}`);

    console.log(`  l1-transition-gasprice: ${gasPriceLog['transition'] ?? 0n}`);
    console.log(`  l1-transition-fee: ${feeLog['transition'] ?? 0n}`);
    console.log(`  l1-transition-calldata-bytes: ${calldataLog['transition'] ?? 0}`);
    console.log(`  l1-transition-input-count: ${inputCountLog['transition'] ?? 0}`);

    console.log(`  l1-terminate-gasprice: ${gasPriceLog['terminate'] ?? 0n}`);
    console.log(`  l1-terminate-fee: ${feeLog['terminate'] ?? 0n}`);
    console.log(`  l1-terminate-calldata-bytes: ${calldataLog['terminate'] ?? 0}`);
    console.log(`  l1-terminate-input-count: ${inputCountLog['terminate'] ?? 0}`);

    console.log(`  l1-deploy-iv-bytes: ${bytecodeSizeLog['InstantiationVerifier'] ?? 0}`);
    console.log(`  l1-deploy-tv-bytes: ${bytecodeSizeLog['TransitionVerifier'] ?? 0}`);
    console.log(`  l1-deploy-rv-bytes: ${bytecodeSizeLog['TerminationVerifier'] ?? 0}`);
    console.log(`  l1-deploy-im-bytes: ${bytecodeSizeLog['InstanceManager'] ?? 0}`);
    console.log(`  l1-deploy-tx-count: 4`);
    if ('InstantiationVerifier' in deployGas)
      console.log(`  l1-deploy-iv-gas: ${deployGas['InstantiationVerifier']}`);
    if ('TransitionVerifier' in deployGas)
      console.log(`  l1-deploy-tv-gas: ${deployGas['TransitionVerifier']}`);
    if ('TerminationVerifier' in deployGas)
      console.log(`  l1-deploy-rv-gas: ${deployGas['TerminationVerifier']}`);
    if ('InstanceManager' in deployGas)
      console.log(`  l1-deploy-im-gas: ${deployGas['InstanceManager']}`);
    if ('InstantiationVerifier' in deployFee)
      console.log(`  l1-deploy-iv-fee: ${deployFee['InstantiationVerifier']}`);
    if ('TransitionVerifier' in deployFee)
      console.log(`  l1-deploy-tv-fee: ${deployFee['TransitionVerifier']}`);
    if ('TerminationVerifier' in deployFee)
      console.log(`  l1-deploy-rv-fee: ${deployFee['TerminationVerifier']}`);
    if ('InstanceManager' in deployFee)
      console.log(`  l1-deploy-im-fee: ${deployFee['InstanceManager']}`);
  });
});
