import hre from "hardhat";
import { Wallet, Provider } from "zksync-ethers";
import { Deployer } from "@matterlabs/hardhat-zksync-deploy";
import * as fs from "fs";
import { expect } from "chai";

// era_test_node rich wallet (pre-funded)
const RICH_PK = "0x7726827caac94a7f9e1b160f7ea819f172f7b6f9d2a97f992c38edeab82d4110";

describe("InstanceManager (zkSync ERA)", function () {
  let instanceManager: any;
  let mockManager: any;
  let proofs: any;
  const gasLog: Record<string, bigint> = {};
  const deployGas: Record<string, bigint> = {};

  before(async function () {
    this.timeout(120_000);
    proofs = JSON.parse(fs.readFileSync("test/proofs.json").toString());

    const provider = new Provider(hre.network.config.url as string);
    const wallet   = new Wallet(RICH_PK, provider);
    const deployer = new Deployer(hre, wallet);

    // Deploy real verifiers + InstanceManager (mirrors InstanceManagerModule)
    const iv = await deployer.deploy(await deployer.loadArtifact("InstantiationVerifier"));
    const tv = await deployer.deploy(await deployer.loadArtifact("TransitionVerifier"));
    const rv = await deployer.deploy(await deployer.loadArtifact("TerminationVerifier"));
    const im = await deployer.deploy(await deployer.loadArtifact("InstanceManager"),
      [await iv.getAddress(), await tv.getAddress(), await rv.getAddress()]);
    instanceManager = im;

    // Log deployment gas
    deployGas["InstantiationVerifier"] = (await iv.deploymentTransaction()!.wait())!.gasUsed;
    deployGas["TransitionVerifier"]    = (await tv.deploymentTransaction()!.wait())!.gasUsed;
    deployGas["TerminationVerifier"]   = (await rv.deploymentTransaction()!.wait())!.gasUsed;
    deployGas["InstanceManager"]       = (await im.deploymentTransaction()!.wait())!.gasUsed;

    // Deploy mock manager (mirrors InstanceManagerWithMockModule)
    const mock = await deployer.deploy(await deployer.loadArtifact("InstantiationVerifierMock"));
    const mm   = await deployer.deploy(await deployer.loadArtifact("InstanceManager"),
      [await mock.getAddress(), await tv.getAddress(), await rv.getAddress()]);
    mockManager = mm;
  });

  it("instantiation", async function () {
    this.timeout(60_000);
    const p = proofs[0];
    const tx = await instanceManager.instantiate(p.value, p.input[0]);
    const r  = await tx.wait();
    gasLog["instantiate (real)"] = r.gasUsed;
    expect(await instanceManager.instances(p.input[0])).to.be.true;
  });

  it("termination", async function () {
    this.timeout(60_000);
    const p = proofs[proofs.length - 1];
    // Use mock for instantiation (mirrors L1 test)
    await (await mockManager.instantiate(p.value, p.input[0])).wait();
    const tx = await mockManager.terminate(p.value, p.input[0]);
    const r  = await tx.wait();
    gasLog["terminate"] = r.gasUsed;
    expect(await mockManager.instances(p.input[0])).to.be.false;
  });

  it("transition", async function () {
    this.timeout(60_000);
    const p = proofs[1];
    await (await mockManager.instantiate(p.value, p.input[0])).wait();
    const tx = await mockManager.transition(p.value, p.input[0], p.input[1]);
    const r  = await tx.wait();
    gasLog["transition"] = r.gasUsed;
    expect(await mockManager.instances(p.input[0])).to.be.false;
    expect(await mockManager.instances(p.input[1])).to.be.true;
  });

  after(function () {
    console.log("\n┌──────────────────────────────────────────────┐");
    console.log("│  zkSync ERA Gas Costs (era_test_node)        │");
    console.log("├───────────────────────┬──────────────────────┤");
    console.log("│  Method               │  Gas Used            │");
    console.log("├───────────────────────┼──────────────────────┤");
    for (const [k, v] of Object.entries(gasLog))
      console.log(`│  ${k.padEnd(21)} │  ${v.toString().padStart(20)} │`);
    console.log("├───────────────────────┴──────────────────────┤");
    console.log("│  Deployments                                  │");
    console.log("├───────────────────────┬──────────────────────┤");
    for (const [k, v] of Object.entries(deployGas))
      console.log(`│  ${k.padEnd(21)} │  ${v.toString().padStart(20)} │`);
    console.log("└───────────────────────┴──────────────────────┘");
  });
});
