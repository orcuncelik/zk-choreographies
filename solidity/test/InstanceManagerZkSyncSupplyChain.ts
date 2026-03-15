/**
 * zkSync gas test for the supply-chain choreography.
 * Uses `test/supply_chain_proofs.json` and expects `anvil-zksync` on port 8011.
 * Run with `npx hardhat test test/InstanceManagerZkSyncSupplyChain.ts --network zkSyncLocalNode`.
 */

import hre from 'hardhat';
import { Wallet, Provider } from 'zksync-ethers';
import { Deployer } from '@matterlabs/hardhat-zksync-deploy';
import * as fs from 'fs';
import { expect } from 'chai';

const abiBytes = (hexData: string) => (hexData.length - 2) / 2;

// Pre-funded anvil-zksync wallet.
const RICH_PK = '0x7726827caac94a7f9e1b160f7ea819f172f7b6f9d2a97f992c38edeab82d4110';

describe('InstanceManager (Supply Chain — zkSync ERA)', function () {
  let instanceManager: any;
  let mockManager: any;
  let provider: Provider;
  let proofs: any[];

  const gasLog:          Record<string, bigint> = {};
  const gasPriceLog:     Record<string, bigint> = {};
  const feeLog:          Record<string, bigint> = {};
  const calldataLog:     Record<string, number> = {};
  const deployGas:       Record<string, bigint> = {};
  const deployFee:       Record<string, bigint> = {};
  const bytecodeSizeLog: Record<string, number> = {};
  const inputCountLog:   Record<string, number> = {};
  const storageWritesLog: Record<string, number> = {};
  let   proofSizeBytes = 0;
  let   chainId: bigint = 0n;

  before(async function () {
    this.timeout(120_000);

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

    provider = new Provider(hre.network.config.url as string);
    const wallet   = new Wallet(RICH_PK, provider);
    const deployer = new Deployer(hre, wallet);

    // Chain ID
    const network = await provider.getNetwork();
    chainId = network.chainId;

    // Deploy real verifiers and manager.
    const iv = await deployer.deploy(await deployer.loadArtifact('InstantiationVerifier'));
    const tv = await deployer.deploy(await deployer.loadArtifact('TransitionVerifier'));
    const rv = await deployer.deploy(await deployer.loadArtifact('TerminationVerifier'));
    const im = await deployer.deploy(await deployer.loadArtifact('InstanceManager'),
      [await iv.getAddress(), await tv.getAddress(), await rv.getAddress()]);
    instanceManager = im;

    // Save deployment gas and fees.
    const ivR = await iv.deploymentTransaction()!.wait();
    const tvR = await tv.deploymentTransaction()!.wait();
    const rvR = await rv.deploymentTransaction()!.wait();
    const imR = await im.deploymentTransaction()!.wait();

    deployGas['InstantiationVerifier'] = ivR!.gasUsed;
    deployGas['TransitionVerifier']    = tvR!.gasUsed;
    deployGas['TerminationVerifier']   = rvR!.gasUsed;
    deployGas['InstanceManager']       = imR!.gasUsed;

    deployFee['InstantiationVerifier'] = ivR!.gasUsed * BigInt(ivR!.gasPrice ?? 0);
    deployFee['TransitionVerifier']    = tvR!.gasUsed * BigInt(tvR!.gasPrice ?? 0);
    deployFee['TerminationVerifier']   = rvR!.gasUsed * BigInt(rvR!.gasPrice ?? 0);
    deployFee['InstanceManager']       = imR!.gasUsed * BigInt(imR!.gasPrice ?? 0);

    // Deployed bytecode sizes
    bytecodeSizeLog['InstantiationVerifier'] = ((await provider.getCode(await iv.getAddress())).length - 2) / 2;
    bytecodeSizeLog['TransitionVerifier']    = ((await provider.getCode(await tv.getAddress())).length - 2) / 2;
    bytecodeSizeLog['TerminationVerifier']   = ((await provider.getCode(await rv.getAddress())).length - 2) / 2;
    bytecodeSizeLog['InstanceManager']       = ((await provider.getCode(await im.getAddress())).length - 2) / 2;

    // Deploy a mock-backed manager.
    const mock = await deployer.deploy(await deployer.loadArtifact('InstantiationVerifierMock'));
    const mm   = await deployer.deploy(await deployer.loadArtifact('InstanceManager'),
      [await mock.getAddress(), await tv.getAddress(), await rv.getAddress()]);
    mockManager = mm;

    // Seed state for transition and termination.
    const pTrans = proofs[1];
    await (await mockManager.instantiate(pTrans.value, pTrans.input[0])).wait();
    const pTerm  = proofs[proofs.length - 1];
    await (await mockManager.instantiate(pTerm.value, pTerm.input[0])).wait();
  });

  it('instantiation (real verifier)', async function () {
    this.timeout(60_000);
    const p  = proofs[0];
    const tx = await instanceManager.instantiate(p.value, p.input[0]);
    const r  = await tx.wait();
    gasLog['instantiate (real)']      = r.gasUsed;
    gasPriceLog['instantiate (real)'] = BigInt(r.gasPrice ?? 0);
    feeLog['instantiate (real)']      = r.gasUsed * BigInt(r.gasPrice ?? 0);

    // ABI payload size only.
    calldataLog['instantiate'] =
      abiBytes(instanceManager.interface.encodeFunctionData('instantiate', [p.value, p.input[0]]));

    // Approximate pubdata as storage writes.
    try {
      const details = await (provider as any).send('zks_getTransactionDetails', [r.hash]);
      const ei = details?.executionInfo;
      if (ei != null) {
        storageWritesLog['instantiate'] =
          (ei.initialStorageChanges ?? 0) + (ei.repeatedStorageChanges ?? 0);
      }
    } catch { /* pubdata API not available */ }

    expect(await instanceManager.instances(p.input[0])).to.be.true;
  });

  it('transition', async function () {
    this.timeout(60_000);
    const p = proofs[1];
    const tx = await mockManager.transition(p.value, p.input[0], p.input[1]);
    const r  = await tx.wait();
    gasLog['transition']      = r.gasUsed;
    gasPriceLog['transition'] = BigInt(r.gasPrice ?? 0);
    feeLog['transition']      = r.gasUsed * BigInt(r.gasPrice ?? 0);

    calldataLog['transition'] =
      abiBytes(mockManager.interface.encodeFunctionData('transition', [p.value, p.input[0], p.input[1]]));

    try {
      const details = await (provider as any).send('zks_getTransactionDetails', [r.hash]);
      const ei = details?.executionInfo;
      if (ei != null) {
        storageWritesLog['transition'] =
          (ei.initialStorageChanges ?? 0) + (ei.repeatedStorageChanges ?? 0);
      }
    } catch { /* pubdata API not available */ }

    expect(await mockManager.instances(p.input[0])).to.be.false;
    expect(await mockManager.instances(p.input[1])).to.be.true;
  });

  it('termination', async function () {
    this.timeout(60_000);
    const p = proofs[proofs.length - 1];
    const tx = await mockManager.terminate(p.value, p.input[0]);
    const r  = await tx.wait();
    gasLog['terminate']      = r.gasUsed;
    gasPriceLog['terminate'] = BigInt(r.gasPrice ?? 0);
    feeLog['terminate']      = r.gasUsed * BigInt(r.gasPrice ?? 0);

    calldataLog['terminate'] =
      abiBytes(mockManager.interface.encodeFunctionData('terminate', [p.value, p.input[0]]));

    try {
      const details = await (provider as any).send('zks_getTransactionDetails', [r.hash]);
      const ei = details?.executionInfo;
      if (ei != null) {
        storageWritesLog['terminate'] =
          (ei.initialStorageChanges ?? 0) + (ei.repeatedStorageChanges ?? 0);
      }
    } catch { /* pubdata API not available */ }

    expect(await mockManager.instances(p.input[0])).to.be.false;
  });

  after(function () {
    const N_TRANSITIONS = 9; // supply chain has 9 choreography tasks

    // Per-operation receipts
    console.log('\n  zkSync ERA — receipt-based gas (authoritative):');
    console.log('┌──────────────────────────┬──────────────┬──────────────────────┐');
    console.log('│  Method                  │  gasUsed     │  fee paid (wei)      │');
    console.log('├──────────────────────────┼──────────────┼──────────────────────┤');
    for (const [k, v] of Object.entries(gasLog)) {
      const price = gasPriceLog[k] ?? 0n;
      const fee   = v * price;
      console.log(`│  ${k.padEnd(24)} │  ${v.toString().padStart(12)} │  ${fee.toString().padStart(20)} │`);
    }

    // Deployment receipts
    console.log('├──────────────────────────┼──────────────┴──────────────────────┤');
    console.log('│  Deployments             │  gasUsed                            │');
    console.log('├──────────────────────────┼─────────────────────────────────────┤');
    for (const [k, v] of Object.entries(deployGas))
      console.log(`│  ${k.padEnd(24)} │  ${v.toString().padStart(35)} │`);
    console.log('└──────────────────────────┴─────────────────────────────────────┘');

    // Totals
    const inst  = gasLog['instantiate (real)'] ?? 0n;
    const trans = gasLog['transition']          ?? 0n;
    const term  = gasLog['terminate']            ?? 0n;
    const execTotal   = inst + trans * BigInt(N_TRANSITIONS) + term;
    const deployTotal = Object.values(deployGas).reduce((a, b) => a + b, 0n);
    const grandTotal  = deployTotal + execTotal;

    const execFeeTotal   = (feeLog['instantiate (real)'] ?? 0n)
                         + (feeLog['transition'] ?? 0n) * BigInt(N_TRANSITIONS)
                         + (feeLog['terminate'] ?? 0n);
    const deployFeeTotal = Object.values(deployFee).reduce((a, b) => a + b, 0n);
    const grandFeeTotal  = deployFeeTotal + execFeeTotal;

    console.log(`\n  Full Supply Chain estimated on-chain cost (zkSync ERA):`);
    console.log(`    instantiate (×1):             ${inst.toLocaleString()}`);
    console.log(`    transition  (×${N_TRANSITIONS}):            ${(trans * BigInt(N_TRANSITIONS)).toLocaleString()}`);
    console.log(`    terminate   (×1):             ${term.toLocaleString()}`);
    console.log(`    ─────────────────────────────────────────`);
    console.log(`    TOTAL:                        ${execTotal.toLocaleString()} gas`);

    // Summary lines for run_choreography.py
    console.log(`\n  zksync-chain-id: ${chainId}`);
    console.log(`  zksync-transition-count: ${N_TRANSITIONS}`);
    console.log(`  zksync-proof-size-bytes: ${proofSizeBytes}`);

    console.log(`  zksync-deploy-total: ${deployTotal.toLocaleString()} gas`);
    console.log(`  zksync-exec-total: ${execTotal.toLocaleString()} gas`);
    console.log(`  zksync-grand-total: ${grandTotal.toLocaleString()} gas`);

    console.log(`  zksync-deploy-total-fee: ${deployFeeTotal}`);
    console.log(`  zksync-exec-total-fee: ${execFeeTotal}`);
    console.log(`  zksync-grand-total-fee: ${grandFeeTotal}`);

    console.log(`  zksync-instantiate-gasprice: ${gasPriceLog['instantiate (real)'] ?? 0n}`);
    console.log(`  zksync-instantiate-fee: ${feeLog['instantiate (real)'] ?? 0n}`);
    console.log(`  zksync-instantiate-calldata-bytes: ${calldataLog['instantiate'] ?? 0}`);
    console.log(`  zksync-instantiate-input-count: ${inputCountLog['instantiate'] ?? 0}`);
    if ('instantiate' in storageWritesLog)
      console.log(`  zksync-instantiate-storage-writes: ${storageWritesLog['instantiate']}`);

    console.log(`  zksync-transition-gasprice: ${gasPriceLog['transition'] ?? 0n}`);
    console.log(`  zksync-transition-fee: ${feeLog['transition'] ?? 0n}`);
    console.log(`  zksync-transition-calldata-bytes: ${calldataLog['transition'] ?? 0}`);
    console.log(`  zksync-transition-input-count: ${inputCountLog['transition'] ?? 0}`);
    if ('transition' in storageWritesLog)
      console.log(`  zksync-transition-storage-writes: ${storageWritesLog['transition']}`);

    console.log(`  zksync-terminate-gasprice: ${gasPriceLog['terminate'] ?? 0n}`);
    console.log(`  zksync-terminate-fee: ${feeLog['terminate'] ?? 0n}`);
    console.log(`  zksync-terminate-calldata-bytes: ${calldataLog['terminate'] ?? 0}`);
    console.log(`  zksync-terminate-input-count: ${inputCountLog['terminate'] ?? 0}`);
    if ('terminate' in storageWritesLog)
      console.log(`  zksync-terminate-storage-writes: ${storageWritesLog['terminate']}`);

    console.log(`  zksync-deploy-iv-bytes: ${bytecodeSizeLog['InstantiationVerifier'] ?? 0}`);
    console.log(`  zksync-deploy-tv-bytes: ${bytecodeSizeLog['TransitionVerifier'] ?? 0}`);
    console.log(`  zksync-deploy-rv-bytes: ${bytecodeSizeLog['TerminationVerifier'] ?? 0}`);
    console.log(`  zksync-deploy-im-bytes: ${bytecodeSizeLog['InstanceManager'] ?? 0}`);
    console.log(`  zksync-deploy-tx-count: 4`);

    console.log(`  zksync-deploy-iv-gas: ${deployGas['InstantiationVerifier'] ?? 0n}`);
    console.log(`  zksync-deploy-tv-gas: ${deployGas['TransitionVerifier'] ?? 0n}`);
    console.log(`  zksync-deploy-rv-gas: ${deployGas['TerminationVerifier'] ?? 0n}`);
    console.log(`  zksync-deploy-im-gas: ${deployGas['InstanceManager'] ?? 0n}`);

    console.log(`  zksync-deploy-iv-fee: ${deployFee['InstantiationVerifier'] ?? 0n}`);
    console.log(`  zksync-deploy-tv-fee: ${deployFee['TransitionVerifier'] ?? 0n}`);
    console.log(`  zksync-deploy-rv-fee: ${deployFee['TerminationVerifier'] ?? 0n}`);
    console.log(`  zksync-deploy-im-fee: ${deployFee['InstanceManager'] ?? 0n}`);
  });
});
