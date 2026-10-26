import { GnomeAccount, Stage } from '@gnome-trading-group/gnome-shared-cdk';

export const GITHUB_REPO = 'gnome-trading-group/gnome-bot';
export const GITHUB_BRANCH = 'main';

// The worker's role has a fixed name in each stage so the pipelines account's read role can trust it by ARN.
export const WORKER_ROLE_NAME = 'gnome-bot-worker';
export const PIPELINES_READ_ROLE_NAME = 'gnome-bot-pipelines-read';

export interface BotConfig {
  account: GnomeAccount;
}

export const CONFIGS: { [stage in Stage]?: BotConfig } = {
  [Stage.DEV]: { account: GnomeAccount.InfraDev },
  [Stage.PROD]: { account: GnomeAccount.InfraProd },
};
