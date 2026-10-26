#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { BotPipelineStack } from '../lib/bot-pipeline-stack';
import { GnomeAccount } from '@gnome-trading-group/gnome-shared-cdk';

const app = new cdk.App();
new BotPipelineStack(app, 'BotPipelineStack', {
  env: GnomeAccount.InfraPipelines.environment,
});
app.synth();
