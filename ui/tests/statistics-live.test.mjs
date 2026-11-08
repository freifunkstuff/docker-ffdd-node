import assert from 'node:assert/strict';
import test from 'node:test';
import { mergeLiveHistory } from '../src/statistics-live.ts';
const frame = timestamp => ({timestamp});
const history = (epoch, updated_at) => ({version:1,epoch,history_seconds:3600,detail_seconds:300,updated_at,resolutions:{'1':[frame(updated_at)],'15':[frame(updated_at)]}});

test('live tail extends only the same history generation', () => {
 const full=history('a',100);
 const merged=mergeLiveHistory(full,{version:1,epoch:'a',updated_at:102,samples:[frame(101),frame(102)]});
 assert.deepEqual(merged.resolutions['1'].map(p=>p.timestamp),[100,101,102]);
 assert.equal(merged.resolutions['15'],full.resolutions['15']);
 assert.deepEqual(full.resolutions['1'].map(p=>p.timestamp),[100]);
});

test('collector restart cannot join old and new rings', () => {
 const merged=mergeLiveHistory(history('a',100),{version:1,epoch:'b',updated_at:101,samples:[frame(101)]});
 assert.equal(merged.epoch,'b');assert.deepEqual(merged.resolutions['1'].map(p=>p.timestamp),[101]);
 assert.deepEqual(merged.resolutions['15'],[]);
});

test('old live response cannot replace a newer full collection', () => {
 const full=history('b',110);
 assert.equal(mergeLiveHistory(full,{version:1,epoch:'a',updated_at:109,samples:[frame(109)]}),full);
 assert.equal(mergeLiveHistory(full,null),full);
});
