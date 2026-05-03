import { app } from 'electron';
import path from 'path';
import fs from 'fs';

interface AppConfig {
    pythonPath?: string;
    serverUrl?: string;
    [key: string]: any;
}

class ConfigService {
    private configPath: string;
    private config: AppConfig = {};

    constructor() {
        // 存储在用户数据目录下的 ai-ide-config.json
        this.configPath = path.join(app.getPath('userData'), 'ai-ide-config.json');
        this.load();
    }

    private load() {
        try {
            if (fs.existsSync(this.configPath)) {
                const data = fs.readFileSync(this.configPath, 'utf-8');
                this.config = JSON.parse(data);
            }
        } catch (error) {
            console.error('[Config Service] Failed to load config:', error);
            this.config = {};
        }
    }

    private save() {
        try {
            const data = JSON.stringify(this.config, null, 2);
            fs.writeFileSync(this.configPath, data, 'utf-8');
        } catch (error) {
            console.error('[Config Service] Failed to save config:', error);
        }
    }

    get(key: keyof AppConfig) {
        return this.config[key];
    }

    set(key: keyof AppConfig, value: any) {
        this.config[key] = value;
        this.save();
    }

    getAll() {
        return { ...this.config };
    }
}

export const configService = new ConfigService();
