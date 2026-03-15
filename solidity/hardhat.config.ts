import { HardhatUserConfig } from "hardhat/config";
import "@nomicfoundation/hardhat-ignition-ethers";
import "@nomicfoundation/hardhat-chai-matchers";
import "@nomicfoundation/hardhat-toolbox";
import "hardhat-gas-reporter";
import "@matterlabs/hardhat-zksync";

const config: HardhatUserConfig = {
  solidity: "0.8.24",
  gasReporter: {
    enabled: process.env.REPORT_GAS === 'true',
  },
  zksolc: {
    version: "1.5.7",
    settings: { optimizer: { enabled: false } },
  },
  networks: {
    zkSyncLocalNode: {
      url: "http://127.0.0.1:8011",
      ethNetwork: "localhost",
      zksync: true,
    },
  },
};

export default config;
