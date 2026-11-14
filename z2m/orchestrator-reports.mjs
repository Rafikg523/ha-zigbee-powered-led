// Optional Zigbee2MQTT 2.14.3 external extension. No Zigbee commands or reads.
// Forward only received ZCL attributes, never merged/optimistic entity state.
export default class OrchestratorReports {
    constructor(zigbee, mqtt, state, publishEntityState, eventBus,
                enableDisableExtension, restartCallback, addExtension, settings) {
        this.mqtt = mqtt;
        this.eventBus = eventBus;
        this.baseTopic = settings.get().mqtt.base_topic;
        this.session = null;
        this.targets = new Set();
        this.sequence = 0;
    }

    start() {
        this.eventBus.onMQTTMessage(this, (event) => this.subscribe(event));
        this.eventBus.onDeviceMessage(this, (event) => this.report(event));
    }

    async subscribe(event) {
        if (event.topic !== `${this.baseTopic}/orchestrator/subscribe`) return;
        let request;
        try { request = JSON.parse(event.message); } catch { return; }
        if (!request || typeof request.session !== 'string' ||
            !/^[0-9a-f]{32}$/.test(request.session) ||
            !Array.isArray(request.targets) || request.targets.length > 16 ||
            request.targets.some(t => !t || !/^0x[0-9a-f]{16}$/.test(t.ieee) ||
                !Number.isInteger(t.endpoint) || t.endpoint < 1 || t.endpoint > 240)) return;
        this.session = request.session;
        this.sequence = 0;
        this.targets = new Set(request.targets.map(t => `${t.ieee}/${t.endpoint}`));
        await this.publish({session: this.session, sequence: 0, type: 'ready'});
    }

    async report(event) {
        if (!this.session || !['attributeReport', 'readResponse'].includes(event.type)) return;
        const ieee = event.device?.ieeeAddr?.toLowerCase();
        const endpoint = event.endpoint?.ID;
        if (!this.targets.has(`${ieee}/${endpoint}`)) return;
        const fields = {genOnOff: ['onOff', 0, 1], genLevelCtrl: ['currentLevel', 0, 254],
                        lightingColorCtrl: ['colorTemperature', 1, 65534]};
        const field = fields[event.cluster];
        if (!field) return;
        const [attribute, min, max] = field;
        const value = event.data?.[attribute];
        if (!Number.isInteger(value) || value < min || value > max) return;
        await this.publish({session: this.session, sequence: ++this.sequence,
            ieee, endpoint, type: event.type, cluster: event.cluster,
            data: {[attribute]: value}});
    }

    async publish(payload) {
        await this.mqtt.publish('orchestrator/report', JSON.stringify(payload),
            {clientOptions: {retain: false, qos: 0}, skipLog: true});
    }

    stop() {
        this.session = null;
        this.targets.clear();
        this.eventBus.removeListeners(this);
    }
}
