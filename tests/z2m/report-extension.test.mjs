import assert from 'node:assert/strict';
import test from 'node:test';
import Extension from '../../z2m/orchestrator-reports.mjs';

function rig() {
    const callbacks = {};
    const published = [];
    const eventBus = {
        onMQTTMessage: (_, fn) => callbacks.mqtt = fn,
        onDeviceMessage: (_, fn) => callbacks.device = fn,
        removeListeners: () => { callbacks.removed = true; },
    };
    const mqtt = {
        publish: async (...args) => published.push(args),
    };
    const settings = {get: () => ({mqtt: {base_topic: 'test/z2m'}})};
    const extension = new Extension(null, mqtt, null, null, eventBus, null, null, null, settings);
    extension.start();
    return {extension, callbacks, published};
}
const session = 'a'.repeat(32);
const target = {ieee: '0x0000000000000001', endpoint: 1};
const subscribe = {session, targets: [target]};
const event = {type: 'attributeReport', device: {ieeeAddr: target.ieee},
               endpoint: {ID: 1}, cluster: 'genOnOff', data: {onOff: 1}};

test('only raw reports for subscribed endpoints, never a set or retained publication', async () => {
    const r = rig();
    await r.callbacks.device(event);
    assert.equal(r.published.length, 0);
    await r.callbacks.mqtt({topic: 'test/z2m/orchestrator/subscribe', message: JSON.stringify(subscribe)});
    assert.equal(JSON.parse(r.published[0][1]).type, 'ready');
    await r.callbacks.device(event);
    const [topic, text, options] = r.published[1];
    assert.equal(topic, 'orchestrator/report');
    assert.deepEqual(JSON.parse(text), {...target, session, sequence: 1,
        type: 'attributeReport', cluster: 'genOnOff', data: {onOff: 1}});
    assert.equal(options.clientOptions.retain, false);
    await r.callbacks.device({...event, endpoint: {ID: 2}});
    await r.callbacks.device({...event, type: 'commandOn'});
    assert.equal(r.published.length, 2);
    r.extension.stop();
    assert.equal(r.callbacks.removed, true);
});

test('malformed requests and unrecognized attributes do not publish proof', async () => {
    const r = rig();
    await r.callbacks.mqtt({topic: 'test/z2m/orchestrator/subscribe', message: '{broken'});
    await r.callbacks.mqtt({topic: 'test/z2m/orchestrator/subscribe', message: JSON.stringify(subscribe)});
    await r.callbacks.device({...event, data: {onOff: true}});
    await r.callbacks.device({...event, cluster: 'genBasic'});
    assert.equal(r.published.length, 1);
});

test('new session replaces the previous one and reports only received attributes', async () => {
    const r = rig();
    await r.callbacks.mqtt({topic: 'test/z2m/orchestrator/subscribe', message: JSON.stringify(subscribe)});
    const next = 'b'.repeat(32);
    await r.callbacks.mqtt({topic: 'test/z2m/orchestrator/subscribe', message: JSON.stringify({...subscribe, session: next})});
    await r.callbacks.device({...event, type: 'readResponse', cluster: 'genLevelCtrl', data: {currentLevel: 100, unrelated: 42}});
    const payload = JSON.parse(r.published.at(-1)[1]);
    assert.equal(payload.session, next);
    assert.equal(payload.sequence, 1);
    assert.deepEqual(payload.data, {currentLevel: 100});
});
