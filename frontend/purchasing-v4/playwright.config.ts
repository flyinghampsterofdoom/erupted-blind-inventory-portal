import {defineConfig} from '@playwright/test';
export default defineConfig({testDir:'browser',workers:1,timeout:60000,use:{baseURL:'http://127.0.0.1:58441',viewport:{width:1600,height:1000},trace:'retain-on-failure'},reporter:[['list'],['junit',{outputFile:'test-results/browser.xml'}]],projects:[{name:'chromium',use:{browserName:'chromium'}},{name:'firefox',use:{browserName:'firefox'}}]});
