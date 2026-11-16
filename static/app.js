// AdaptivePlan Single Page Application Controller (Alpine.js + FullCalendar + Chart.js)

// Local (not UTC) YYYY-MM-DD. toISOString() returns the UTC date, which is "yesterday"
// for users in IST between 00:00 and 05:30 and made the Today tab show the wrong day.
function localDate(d = new Date()) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

const NAV_ITEMS = [
  { id: 'dashboard', label: 'Dashboard',       short: 'Home',   icon: 'dashboard', title: 'Dashboard' },
  { id: 'today',     label: 'Today',           short: 'Today',  icon: 'today',     title: 'Today' },
  { id: 'plan',      label: 'Plan & calendar', short: 'Plan',   icon: 'calendar',  title: 'Plan & calendar' },
  { id: 'agent',     label: 'Agent activity',  short: 'Agent',  icon: 'agent',     title: 'Agent activity' },
  { id: 'quiz',      label: 'Quiz & mastery',  short: 'Quiz',   icon: 'quiz',      title: 'Quiz & mastery' },
  { id: 'setup',     label: 'Setup & syllabus', short: 'Setup', icon: 'setup',     title: 'Setup & syllabus' }
];

// Non-reactive holders for third-party widget instances
const _raw = { calendar: null, taskTypeChart: null, dailyHoursChart: null };

function adaptiveApp() {
  return {
    activeTab: 'dashboard',
    navItems: NAV_ITEMS,
    darkMode: (() => {
      const s = localStorage.getItem('adaptive_dark');
      return s === null ? window.matchMedia('(prefers-color-scheme: dark)').matches : s === 'true';
    })(),
    status: {
      mock_mode: true,
      llm_mode: 'mock',
      live_available: false,
      model: 'Mock LLM Provider',
      is_demo: false,
      subjects_count: 0,
      topics_count: 0,
      tasks_count: 0
    },

    // Mode & Connection state
    testingConnection: false,
    connectionTestResult: null,

    // Data structures
    todayStr: localDate(),
    dashboard: { readiness: [], summary: {}, task_types: {}, daily_hours: [] },
    coachReview: { score: 85, issues: [], suggestions: [], encouragement: '', burnout_warning: null },
    checkin: { message: '', date: '', task_count: 0 },
    tasks: [],
    todayTasks: [],
    selectedTask: null,
    setupData: { profile: { max_daily_hours: 4.0, preferred_time: 'evening', break_minutes: 15, burnout_threshold_days: 5 }, free_slots: [], subjects: [] },
    allTopics: [],

    // Agent live activity
    activeRunId: null,
    agentWorking: false,
    agentEvents: [],
    eventSource: null,
    lastRunProvider: '',
    lastRunModel: '',
    lastRunFallback: false,
    fallbackReason: '',

    // Agent Run History
    agentRuns: [],
    historyPanelOpen: false,
    viewingHistoryRunId: null,

    // Explanation Modal
    explainModalOpen: false,
    explanationData: null,

    // Replan Delta Banner
    replanDelta: null,

    // Quiz Flow
    selectedQuizTopicId: '',
    activeQuiz: null,
    quizLoading: false,
    quizSubmitted: false,
    userAnswers: [],
    quizResult: null,

    // Syllabus & Past Paper
    syllabusText: '',
    pastPaperText: '',
    pastPaperResult: null,

    // New Plan Confirm Dialog & Wizard
    newPlanConfirmOpen: false,
    wizardOpen: false,
    wizardStep: 1,
    wizardSubjects: [],
    wizardActiveSubjIdx: 0,
    wizardSyllabusText: '',
    wizardPastPaperText: '',
    wizardParsing: false,
    wizardPastPaperAnalyzing: false,
    bulkUploading: false,
    bulkPasteOpen: false,
    bulkPasteText: '',
    bulkSuccessMessage: '',
    wizardProfile: {
      max_daily_hours: 4.0,
      preferred_time: 'evening',
      break_minutes: 15,
      burnout_threshold_days: 5
    },

    // Subject/Topic Edit Modals
    editingSubject: null,
    editingTopic: null,
    addingTopicSubjId: null,
    newTopicForm: { unit: 'Unit 1', name: '', difficulty: 3, est_hours: 2.0, weightage: 1.0 },

    // Charts & Calendar handles live in the module-level `_raw` object (see top of file):
    // Alpine's reactive proxy wraps any object it returns — even from getters — and Chart.js /
    // FullCalendar break when wrapped, so they are always accessed as _raw.<name>.

    // UI chrome: toasts + in-app confirm/prompt dialog (replaces alert/confirm/prompt)
    toasts: [],
    _toastId: 0,
    dialog: null,
    wizardStepLabels: ['Subjects', 'Syllabus', 'Preferences', 'Past papers', 'Review'],

    get hasData() {
      return (this.tasks && this.tasks.length > 0) ||
             (this.status && (this.status.subjects_count || 0) > 0) ||
             (this.setupData && this.setupData.subjects && this.setupData.subjects.length > 0);
    },

    get unscheduledConflict() {
      const c = (this.dashboard?.conflicts || []).find(x => x.type === 'unscheduled_topics');
      if (c) return c;
      if (this.replanDelta?.unscheduled_topics?.length > 0) {
        const count = this.replanDelta.unscheduled_topics.length;
        const hrs = this.replanDelta.unscheduled_hours || 0;
        return {
          message: `${count} topics could not fit before the exam (${hrs}h unscheduled). Increase daily hours or use Panic Mode.`
        };
      }
      return null;
    },

    promptPanicFromWarning() {
      if (this.setupData?.subjects?.length > 0) {
        const first = this.setupData.subjects[0];
        this.triggerPanicMode(first.id, first.name);
      } else {
        this.switchTab('setup');
      }
    },

    get pageTitle() {
      const item = NAV_ITEMS.find(n => n.id === this.activeTab);
      return item ? item.title : 'AdaptivePlan';
    },

    get todayLabel() {
      return new Date().toLocaleDateString('en-IN', { weekday: 'long', day: 'numeric', month: 'long' });
    },

    get todayDone() {
      return this.todayTasks.filter(t => t.status === 'done').length;
    },

    get nextTasks() {
      return this.todayTasks
        .filter(t => t.status !== 'done')
        .sort((a, b) => a.start_time.localeCompare(b.start_time))
        .slice(0, 3);
    },

    get visibleAgentEvents() {
      const shown = ['thought', 'tool_call', 'tool_result', 'summary', 'error'];
      return this.agentEvents.filter(e => shown.includes(e.type));
    },

    get answeredCount() {
      return (this.userAnswers || []).filter(a => a).length;
    },

    get wizardTitle() {
      return ['Subjects & exam dates', 'Syllabus topics', 'Preferences & guardrails', 'Past-paper weightage', 'Review & generate'][this.wizardStep - 1] || '';
    },

    // ----------------- UI helpers -----------------

    scoreClass(score) {
      return score >= 75 ? 'score-good' : (score >= 50 ? 'score-mid' : 'score-low');
    },

    daysClass(days) {
      return days <= 3 ? 'chip-bad' : (days <= 7 ? 'chip-warn' : '');
    },

    ringDash(score) {
      const c = 2 * Math.PI * 26;
      const pct = Math.max(0, Math.min(100, Number(score) || 0)) / 100;
      return `${(pct * c).toFixed(1)} ${c.toFixed(1)}`;
    },

    agentKind(type) {
      return { tool_call: 'call', tool_result: 'result' }[type] || type;
    },

    agentIcon(type) {
      return { thought: 'thought', tool_call: 'terminal', tool_result: 'check', summary: 'flag', error: 'alert' }[type] || 'thought';
    },

    agentLabel(type) {
      return { thought: 'Thought', tool_call: 'Tool call', tool_result: 'Result', summary: 'Summary', error: 'Error' }[type] || type;
    },

    toast(message, type = 'info', ms) {
      const id = ++this._toastId;
      this.toasts.push({ id, message, type });
      setTimeout(() => this.dismissToast(id), ms || (type === 'error' ? 6500 : 4200));
    },

    dismissToast(id) {
      this.toasts = this.toasts.filter(t => t.id !== id);
    },

    askConfirm(opts = {}) {
      return new Promise(resolve => {
        this.dialog = {
          title: opts.title || 'Are you sure?',
          message: opts.message || '',
          confirmLabel: opts.confirmLabel || 'Confirm',
          danger: !!opts.danger,
          input: null,
          resolve
        };
      });
    },

    askInput(opts = {}) {
      return new Promise(resolve => {
        this.dialog = {
          title: opts.title || 'Enter a value',
          message: opts.message || '',
          confirmLabel: opts.confirmLabel || 'Continue',
          danger: false,
          input: { label: opts.label || '', value: opts.value ?? '', type: opts.type || 'text', min: opts.min, max: opts.max },
          resolve
        };
        this.$nextTick(() => {
          const el = document.getElementById('dialog-input');
          if (el) { el.focus(); if (el.select) el.select(); }
        });
      });
    },

    closeDialog(ok) {
      const d = this.dialog;
      if (!d) return;
      this.dialog = null;
      d.resolve(d.input ? (ok ? d.input.value : null) : !!ok);
    },

    closeTopLayer() {
      if (this.dialog) return this.closeDialog(false);
      if (this.explainModalOpen) { this.explainModalOpen = false; return; }
      if (this.editingTopic) { this.editingTopic = null; return; }
      if (this.addingTopicSubjId) { this.addingTopicSubjId = null; return; }
      if (this.editingSubject) { this.editingSubject = null; return; }
      if (this.newPlanConfirmOpen) { this.newPlanConfirmOpen = false; return; }
    },

    async apiError(res, fallback = 'Something went wrong. Please try again.') {
      try {
        const body = await res.json();
        if (typeof body.detail === 'string') return body.detail;
        if (Array.isArray(body.detail) && body.detail[0]?.msg) return body.detail[0].msg;
      } catch (e) { /* ignore */ }
      return fallback;
    },

    async initApp() {
      document.documentElement.classList.toggle('dark', this.darkMode);

      // Restore the last tab from URL hash or localStorage
      const savedTab = localStorage.getItem('adaptive_active_tab');
      const fromHash = () => {
        const h = location.hash.replace('#', '');
        if (NAV_ITEMS.some(n => n.id === h)) {
          this.activeTab = h;
        } else if (savedTab && NAV_ITEMS.some(n => n.id === savedTab)) {
          this.activeTab = savedTab;
        }
      };
      fromHash();
      window.addEventListener('hashchange', () => { fromHash(); this.afterTabChange(); });

      await this.fetchStatus();
      await this.loadAllData();
      await this.fetchAgentRuns();
      if (this.agentRuns && this.agentRuns.length > 0 && (!this.agentEvents || this.agentEvents.length === 0)) {
        await this.loadAgentRun(this.agentRuns[0].id);
      }
      this.initCalendar();
      this.initCharts();
      this.afterTabChange();
    },

    toggleDarkMode() {
      this.darkMode = !this.darkMode;
      localStorage.setItem('adaptive_dark', this.darkMode);
      document.documentElement.classList.toggle('dark', this.darkMode);
      this.applyChartTheme();
      if (_raw.calendar) _raw.calendar.render();
    },

    switchTab(tab) {
      if (!NAV_ITEMS.some(n => n.id === tab)) return;
      this.activeTab = tab;
      localStorage.setItem('adaptive_active_tab', tab);
      history.replaceState(null, '', '#' + tab);
      window.scrollTo({ top: 0 });
      this.afterTabChange();
    },

    afterTabChange() {
      if (this.activeTab === 'plan') {
        setTimeout(() => {
          if (_raw.calendar) {
            _raw.calendar.render();
            _raw.calendar.updateSize();
            this.scrollToEarliestSession();
          }
        }, 100);
      } else if (this.activeTab === 'dashboard') {
        setTimeout(() => this.updateCharts(), 100);
      }
    },

    // ----------------- PART A: LLM Mode & Connection -----------------

    async fetchStatus() {
      try {
        const res = await fetch('/api/status');
        if (res.ok) {
          this.status = await res.json();
        }
      } catch (e) {
        console.error('Status fetch error:', e);
      }
    },

    async switchLlmMode(mode) {
      if (mode === 'live' && !this.status.live_available) {
        this.toast('Live mode is locked. Add GEMINI_API_KEY to your .env file to enable Gemini.', 'warn');
        return;
      }
      try {
        const res = await fetch('/api/llm/mode', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mode })
        });
        if (res.ok) {
          const data = await res.json();
          this.status.llm_mode = data.mode;
          this.status.mock_mode = (data.mode === 'mock');
          this.status.model = data.model;
          await this.fetchStatus();
        } else {
          const err = await res.json();
          this.toast(err.detail || 'Failed to switch LLM mode', 'error');
        }
      } catch (e) {
        console.error('Mode switch error:', e);
        this.toast('Could not reach the server. Please try again.', 'error');
      }
    },

    async testConnection() {
      this.testingConnection = true;
      this.connectionTestResult = null;
      try {
        const res = await fetch('/api/llm/test', { method: 'POST' });
        if (res.ok) {
          this.connectionTestResult = await res.json();
        } else {
          this.connectionTestResult = { success: false, message: 'Server returned error' };
        }
      } catch (e) {
        this.connectionTestResult = { success: false, message: e.message };
      } finally {
        this.testingConnection = false;
        setTimeout(() => {
          this.connectionTestResult = null;
        }, 6000);
      }
    },

    // ----------------- PART B: Plan Creation, Reset & Wizard -----------------

    promptNewPlan() {
      if (this.hasData) {
        this.newPlanConfirmOpen = true;
      } else {
        this.startWizardFresh();
      }
    },

    downloadBackup() {
      window.location.href = '/api/backup';
    },

    async confirmResetAndStartWizard() {
      try {
        const res = await fetch('/api/plan/new', { method: 'POST' });
        if (res.ok) {
          this.newPlanConfirmOpen = false;
          await this.loadAllData();
          await this.fetchStatus();
          this.startWizardFresh();
        }
      } catch (e) {
        console.error('Failed to reset plan:', e);
        this.toast('Could not reach the server. Please try again.', 'error');
      }
    },

    startWizardFresh() {
      const today = new Date();
      const d1 = new Date(today); d1.setDate(d1.getDate() + 14);
      const d2 = new Date(today); d2.setDate(d2.getDate() + 21);
      
      this.wizardStep = 1;
      this.bulkSuccessMessage = '';
      this.bulkPasteOpen = false;
      this.bulkPasteText = '';

      // Check for saved local draft
      const savedDraft = localStorage.getItem('adaptive_wizard_draft');
      if (savedDraft) {
        try {
          const parsed = JSON.parse(savedDraft);
          if (Array.isArray(parsed) && parsed.length > 0) {
            this.wizardSubjects = parsed;
            this.bulkSuccessMessage = `✓ Restored saved draft with ${parsed.length} subjects.`;
            this.wizardActiveSubjIdx = 0;
            this.wizardSyllabusText = '';
            this.wizardPastPaperText = '';
            this.wizardOpen = true;
            return;
          }
        } catch (e) {}
      }

      this.wizardSubjects = [
        {
          name: 'Distributed Systems',
          exam_date: localDate(d1),
          priority: 1,
          color: '#3A5BA0',
          topics: [
            { unit: 'Unit 1', name: 'RPC and RMI Architecture', difficulty: 3, est_hours: 2.0, weightage: 1.0 },
            { unit: 'Unit 2', name: 'Lamport Timestamps & Clock Sync', difficulty: 4, est_hours: 2.5, weightage: 1.2 }
          ]
        },
        {
          name: 'Cloud Computing',
          exam_date: localDate(d2),
          priority: 2,
          color: '#7A4FA3',
          topics: [
            { unit: 'Unit 1', name: 'Virtualization & Hypervisors', difficulty: 2, est_hours: 2.0, weightage: 1.0 },
            { unit: 'Unit 2', name: 'Kubernetes Cluster Architecture', difficulty: 4, est_hours: 3.0, weightage: 1.3 }
          ]
        }
      ];
      this.wizardActiveSubjIdx = 0;
      this.wizardSyllabusText = '';
      this.wizardPastPaperText = '';
      this.wizardOpen = true;
    },

    saveWizardDraft() {
      try {
        localStorage.setItem('adaptive_wizard_draft', JSON.stringify(this.wizardSubjects));
      } catch (e) {}
    },

    clearWizardDraft() {
      localStorage.removeItem('adaptive_wizard_draft');
      this.bulkSuccessMessage = '';
      this.toast('Draft cleared.', 'info');
    },

    async uploadBulkSyllabusFile(event) {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      this.bulkUploading = true;
      this.bulkSuccessMessage = '';
      try {
        const formData = new FormData();
        formData.append('file', file);
        const res = await fetch('/api/syllabus/bulk', {
          method: 'POST',
          body: formData
        });
        if (!res.ok) {
          const err = await res.json();
          throw new Error(err.detail || 'Failed to parse syllabus file');
        }
        const data = await res.json();
        this.wizardSubjects = data.subjects || [];
        this.wizardActiveSubjIdx = 0;
        this.bulkSuccessMessage = `✓ Detected ${data.subjects_count} subjects and ${data.total_topics} topics! Review below and adjust exam dates.`;
        this.toast(`Detected ${data.subjects_count} subjects & ${data.total_topics} topics!`, 'success');
        this.saveWizardDraft();
      } catch (err) {
        console.error('Bulk upload error:', err);
        this.toast(err.message, 'error');
      } finally {
        this.bulkUploading = false;
        event.target.value = '';
      }
    },

    async parseBulkSyllabusText() {
      if (!this.bulkPasteText.trim()) return;
      this.bulkUploading = true;
      this.bulkSuccessMessage = '';
      try {
        const formData = new FormData();
        formData.append('text', this.bulkPasteText);
        const res = await fetch('/api/syllabus/bulk', {
          method: 'POST',
          body: formData
        });
        if (!res.ok) {
          const err = await res.json();
          throw new Error(err.detail || 'Failed to parse syllabus text');
        }
        const data = await res.json();
        this.wizardSubjects = data.subjects || [];
        this.wizardActiveSubjIdx = 0;
        this.bulkSuccessMessage = `✓ Detected ${data.subjects_count} subjects and ${data.total_topics} topics! Review below and adjust exam dates.`;
        this.toast(`Detected ${data.subjects_count} subjects & ${data.total_topics} topics!`, 'success');
        this.bulkPasteOpen = false;
        this.bulkPasteText = '';
        this.saveWizardDraft();
      } catch (err) {
        console.error('Bulk parse error:', err);
        this.toast(err.message, 'error');
      } finally {
        this.bulkUploading = false;
      }
    },

    exportSyllabusJson() {
      const payload = {
        exported_at: new Date().toISOString(),
        subjects: this.wizardSubjects
      };
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'adaptiveplan_syllabus.json';
      a.click();
      URL.revokeObjectURL(url);
      this.toast('Syllabus exported to adaptiveplan_syllabus.json', 'success');
    },

    importSyllabusJson(event) {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = (e) => {
        try {
          const data = JSON.parse(e.target.result);
          const subjects = data.subjects || data;
          if (Array.isArray(subjects) && subjects.length > 0) {
            this.wizardSubjects = subjects;
            this.wizardActiveSubjIdx = 0;
            this.bulkSuccessMessage = `✓ Loaded ${subjects.length} subjects from JSON backup!`;
            this.toast(`Imported ${subjects.length} subjects from JSON!`, 'success');
            this.saveWizardDraft();
          } else {
            this.toast('Invalid syllabus JSON format.', 'warn');
          }
        } catch (err) {
          this.toast('Could not parse JSON file: ' + err.message, 'error');
        } finally {
          event.target.value = '';
        }
      };
      reader.readAsText(file);
    },

    addWizardSubject() {
      const today = new Date();
      const futureDate = new Date(today);
      futureDate.setDate(futureDate.getDate() + 20 + this.wizardSubjects.length * 5);
      const colors = ['#2F7D5B', '#C4552D', '#B7791F', '#2E8BA6', '#B14A7A'];
      const color = colors[this.wizardSubjects.length % colors.length];

      this.wizardSubjects.push({
        name: `Subject ${this.wizardSubjects.length + 1}`,
        exam_date: localDate(futureDate),
        priority: 1,
        color: color,
        topics: []
      });
    },

    removeWizardSubject(idx) {
      if (this.wizardSubjects.length <= 1) {
        this.toast('You must have at least one subject.', 'warn');
        return;
      }
      this.wizardSubjects.splice(idx, 1);
      if (this.wizardActiveSubjIdx >= this.wizardSubjects.length) {
        this.wizardActiveSubjIdx = Math.max(0, this.wizardSubjects.length - 1);
      }
    },

    addWizardTopic(subjIdx) {
      const subj = this.wizardSubjects[subjIdx];
      subj.topics.push({
        unit: `Unit ${subj.topics.length + 1}`,
        name: `New Topic ${subj.topics.length + 1}`,
        difficulty: 3,
        est_hours: 2.0,
        weightage: 1.0
      });
    },

    removeWizardTopic(subjIdx, topIdx) {
      this.wizardSubjects[subjIdx].topics.splice(topIdx, 1);
    },

    async parseSyllabusForWizard(subjIdx) {
      const subj = this.wizardSubjects[subjIdx];
      if (!this.wizardSyllabusText.trim()) {
        this.toast('Please paste syllabus text first.', 'warn');
        return;
      }
      this.wizardParsing = true;
      try {
        const formData = new FormData();
        formData.append('text', this.wizardSyllabusText.trim());
        formData.append('subject_name', subj.name);
        formData.append('save_to_db', 'false');
        
        const res = await fetch('/api/syllabus', {
          method: 'POST',
          body: formData
        });
        if (res.ok) {
          const data = await res.json();
          // Extract topics from response flexibly
          const subjectsList = data.subjects || data.extracted_data?.subjects || [];
          let parsedTopics = [];
          if (subjectsList.length > 0 && subjectsList[0].topics && subjectsList[0].topics.length > 0) {
            parsedTopics = subjectsList[0].topics;
          } else if (Array.isArray(data.topics) && data.topics.length > 0) {
            parsedTopics = data.topics;
          } else if (Array.isArray(data.extracted_data?.topics) && data.extracted_data.topics.length > 0) {
            parsedTopics = data.extracted_data.topics;
          }

          if (parsedTopics.length > 0) {
            subj.topics = parsedTopics;
            this.toast(`Extracted ${parsedTopics.length} topics for ${subj.name}.`, 'success');
            this.wizardSyllabusText = '';
          } else {
            this.toast('No topics could be detected. Try adding more structure, e.g. "Unit 1: …".', 'warn');
          }
        } else {
          this.toast('Failed to parse syllabus.', 'error');
        }
      } catch (e) {
        console.error('Syllabus parsing error:', e);
        this.toast('Could not reach the server. Please try again.', 'error');
      } finally {
        this.wizardParsing = false;
      }
    },

    async analyzePastPaperForWizard() {
      if (!this.wizardPastPaperText.trim()) return;
      this.wizardPastPaperAnalyzing = true;
      try {
        const res = await fetch('/api/syllabus/weightage', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: this.wizardPastPaperText })
        });
        if (res.ok) {
          const result = await res.json();
          this.toast(`Past paper analyzed! Identified ${result.high_yield_topics_found || 0} high-yield topics.`, 'success');
          this.wizardStep = 5;
        }
      } catch (e) {
        console.error('Past paper analysis error:', e);
        this.toast('Could not reach the server. Please try again.', 'error');
      } finally {
        this.wizardPastPaperAnalyzing = false;
      }
    },

    nextWizardStep() {
      const todayStr = localDate();

      // Validate Step 1
      if (this.wizardStep === 1) {
        if (this.wizardSubjects.length === 0) {
          this.toast('Add at least one subject.', 'warn');
          return;
        }
        for (const s of this.wizardSubjects) {
          if (!s.name.trim()) {
            this.toast('Subject names cannot be empty.', 'warn');
            return;
          }
          if (s.exam_date <= todayStr) {
            this.toast(`Exam date for "${s.name}" must be in the future (after today).`, 'warn');
            return;
          }
        }
      }

      // Validate Step 2
      if (this.wizardStep === 2) {
        for (const s of this.wizardSubjects) {
          if (!s.topics || s.topics.length === 0) {
            this.toast(`Subject "${s.name}" must have at least one topic. Please add topics or parse syllabus.`, 'warn');
            return;
          }
        }
      }

      // Validate Step 3
      if (this.wizardStep === 3) {
        const hours = this.wizardProfile.max_daily_hours;
        if (hours < 0.5 || hours > 16) {
          this.toast('Max daily hours must be between 0.5 and 16.', 'warn');
          return;
        }
      }

      if (this.wizardStep < 5) {
        this.wizardStep++;
      }
    },

    prevWizardStep() {
      if (this.wizardStep > 1) {
        this.wizardStep--;
      }
    },

    async completeWizardAndGenerate() {
      try {
        // Construct standard free slots based on energy preference
        const freeSlots = [];
        const energy = 'high';
        for (let day = 0; day <= 6; day++) {
          if (day <= 4) {
            // Weekdays: evening slot
            freeSlots.push({ weekday: day, start_time: '18:00', end_time: '22:00', energy: energy });
          } else {
            // Weekends: morning and afternoon slots
            freeSlots.push({ weekday: day, start_time: '10:00', end_time: '14:00', energy: energy });
            freeSlots.push({ weekday: day, start_time: '16:00', end_time: '20:00', energy: 'low' });
          }
        }

        const setupPayload = {
          profile: this.wizardProfile,
          free_slots: freeSlots,
          subjects: this.wizardSubjects.map(s => ({
            name: s.name,
            exam_date: s.exam_date,
            priority: s.priority || 1,
            color: s.color || '#3A5BA0',
            topics: s.topics.map(t => ({
              unit: t.unit || 'Unit 1',
              name: t.name,
              difficulty: parseInt(t.difficulty) || 3,
              est_hours: parseFloat(t.est_hours) || 2.0,
              weightage: parseFloat(t.weightage) || 1.0,
              mastery: 0.0,
              status: 'pending'
            }))
          }))
        };

        const res = await fetch('/api/setup', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(setupPayload)
        });

        if (!res.ok) {
          const err = await res.json();
          this.toast('Error saving setup: ' + (err.detail || JSON.stringify(err)), 'error');
          return;
        }

        this.wizardOpen = false;
        await this.loadAllData();
        await this.fetchStatus();

        // Trigger autonomous agent generation
        await this.triggerGeneratePlan();
      } catch (e) {
        console.error('Wizard completion error:', e);
        this.toast('Failed to generate plan: ' + e.message, 'error');
      }
    },

    // ----------------- Subject & Topic CRUD (Setup Tab) -----------------

    openEditSubject(subj) {
      this.editingSubject = JSON.parse(JSON.stringify(subj));
    },

    async saveEditSubject() {
      if (!this.editingSubject) return;
      try {
        const res = await fetch(`/api/subjects/${this.editingSubject.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: this.editingSubject.name,
            exam_date: this.editingSubject.exam_date,
            priority: this.editingSubject.priority,
            color: this.editingSubject.color
          })
        });
        if (res.ok) {
          const data = await res.json();
          if (data.exam_date_changed && data.replan_details) {
            this.replanDelta = data.replan_details;
          }
          this.editingSubject = null;
          await this.loadAllData();
          await this.fetchStatus();
          this.toast(data.exam_date_changed ? 'Subject updated — schedule replanned.' : 'Subject updated.', 'success');
        } else {
          const err = await res.json();
          this.toast(err.detail || 'Failed to update subject', 'error');
        }
      } catch (e) {
        console.error('Edit subject error:', e);
        this.toast('Could not reach the server. Please try again.', 'error');
      }
    },

    async deleteSubject(subjId) {
      if (!(await this.askConfirm({
        title: 'Delete this subject?',
        message: 'The subject, all of its topics and their scheduled sessions will be removed.',
        confirmLabel: 'Delete subject',
        danger: true
      }))) return;
      try {
        const res = await fetch(`/api/subjects/${subjId}`, { method: 'DELETE' });
        if (res.ok) {
          const data = await res.json();
          if (data.replan_details) {
            this.replanDelta = data.replan_details;
          }
          await this.loadAllData();
          await this.fetchStatus();
        }
      } catch (e) {
        console.error('Delete subject error:', e);
        this.toast('Could not reach the server. Please try again.', 'error');
      }
    },

    openAddTopic(subjId) {
      this.addingTopicSubjId = subjId;
      this.newTopicForm = { unit: 'Unit 1', name: '', difficulty: 3, est_hours: 2.0, weightage: 1.0 };
    },

    async saveNewTopic() {
      if (!this.newTopicForm.name.trim()) {
        this.toast('Please enter a topic name.', 'warn');
        return;
      }
      try {
        const res = await fetch('/api/topics', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            subject_id: this.addingTopicSubjId,
            unit: this.newTopicForm.unit,
            name: this.newTopicForm.name,
            difficulty: parseInt(this.newTopicForm.difficulty),
            est_hours: parseFloat(this.newTopicForm.est_hours),
            weightage: parseFloat(this.newTopicForm.weightage)
          })
        });
        if (res.ok) {
          this.addingTopicSubjId = null;
          await this.loadAllData();
          await this.fetchStatus();
          this.toast('Topic added.', 'success');
        } else {
          this.toast(await this.apiError(res, 'Could not add the topic.'), 'error');
        }
      } catch (e) {
        console.error('Add topic error:', e);
        this.toast('Could not reach the server. Please try again.', 'error');
      }
    },

    openEditTopic(top) {
      this.editingTopic = JSON.parse(JSON.stringify(top));
    },

    async saveEditTopic() {
      if (!this.editingTopic) return;
      try {
        const res = await fetch(`/api/topics/${this.editingTopic.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            unit: this.editingTopic.unit,
            name: this.editingTopic.name,
            difficulty: parseInt(this.editingTopic.difficulty),
            est_hours: parseFloat(this.editingTopic.est_hours),
            weightage: parseFloat(this.editingTopic.weightage),
            mastery: parseFloat(this.editingTopic.mastery || 0)
          })
        });
        if (res.ok) {
          this.editingTopic = null;
          await this.loadAllData();
          this.toast('Topic updated.', 'success');
        } else {
          this.toast(await this.apiError(res, 'Could not update the topic.'), 'error');
        }
      } catch (e) {
        console.error('Edit topic error:', e);
        this.toast('Could not reach the server. Please try again.', 'error');
      }
    },

    async deleteTopic(topId) {
      if (!(await this.askConfirm({
        title: 'Delete this topic?',
        message: 'Its scheduled sessions will be removed too.',
        confirmLabel: 'Delete topic',
        danger: true
      }))) return;
      try {
        const res = await fetch(`/api/topics/${topId}`, { method: 'DELETE' });
        if (res.ok) {
          await this.loadAllData();
          await this.fetchStatus();
        }
      } catch (e) {
        console.error('Delete topic error:', e);
        this.toast('Could not reach the server. Please try again.', 'error');
      }
    },

    // ----------------- Data Loading -----------------

    async loadAllData() {
      await Promise.all([
        this.fetchStatus(),
        this.fetchDashboard(),
        this.fetchCoachFeedback(),
        this.fetchCheckin(),
        this.fetchPlan(),
        this.fetchSetupData()
      ]);
    },

    async fetchDashboard() {
      try {
        const res = await fetch('/api/dashboard');
        if (res.ok) {
          this.dashboard = await res.json();
          this.updateCharts();
        }
      } catch (e) {
        console.error('Dashboard fetch error:', e);
      }
    },

    async fetchCoachFeedback() {
      try {
        const res = await fetch('/api/coach');
        if (res.ok) this.coachReview = await res.json();
      } catch (e) {
        console.error('Coach fetch error:', e);
      }
    },

    async fetchCheckin() {
      try {
        const res = await fetch('/api/checkin');
        if (res.ok) this.checkin = await res.json();
      } catch (e) {
        console.error('Checkin fetch error:', e);
      }
    },

    async fetchPlan() {
      try {
        const res = await fetch('/api/plan');
        if (res.ok) {
          const data = await res.json();
          this.tasks = data.tasks || [];
          this.todayTasks = this.tasks.filter(t => t.date === this.todayStr);
          if (this.selectedTask) {
            this.selectedTask = this.tasks.find(t => t.id === this.selectedTask.id) || null;
          }
          this.updateCalendarEvents();
        }
      } catch (e) {
        console.error('Plan fetch error:', e);
      }
    },

    async fetchSetupData() {
      try {
        const res = await fetch('/api/setup/data');
        if (res.ok) {
          this.setupData = await res.json();
          const topics = [];
          for (const s of (this.setupData.subjects || [])) {
            for (const t of (s.topics || [])) {
              topics.push({
                ...t,
                subject_name: s.name,
                subject_id: s.id
              });
            }
          }
          this.allTopics = topics;
        }
      } catch (e) {
        console.error('Setup data fetch error:', e);
      }
    },

    // ----------------- Theme helpers -----------------

    cssVar(name) {
      return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    },

    // ----------------- Calendar Integration -----------------

    initCalendar() {
      const el = document.getElementById('calendar');
      if (!el) return;
      const narrow = window.innerWidth < 700;

      _raw.calendar = new FullCalendar.Calendar(el, {
        initialView: narrow ? 'timeGridDay' : 'timeGridWeek',
        headerToolbar: narrow
          ? { left: 'prev,next', center: 'title', right: 'today' }
          : { left: 'prev,next today', center: 'title', right: 'dayGridMonth,timeGridWeek,timeGridDay' },
        buttonText: { today: 'Today', month: 'Month', week: 'Week', day: 'Day' },
        firstDay: 1,
        slotMinTime: '07:00:00',
        slotMaxTime: '23:30:00',
        scrollTime: '17:30:00',
        allDaySlot: false,
        nowIndicator: true,
        expandRows: true,
        height: 640,
        dayMaxEvents: 3,
        eventDisplay: 'block',
        events: [],
        eventContent: (arg) => {
          const p = arg.event.extendedProps;
          const wrap = document.createElement('div');
          const title = document.createElement('div');
          title.className = 'ev-title';
          title.textContent = p.topic;
          const sub = document.createElement('div');
          sub.className = 'ev-sub';
          sub.textContent = p.subject + (p.type === 'revision' ? ' · revision' : '');
          wrap.append(title, sub);
          return { domNodes: [wrap] };
        },
        eventDidMount: (info) => {
          info.el.style.setProperty('--ev', info.event.extendedProps.color);
          info.el.title = `${info.event.extendedProps.subject}: ${info.event.extendedProps.topic}`;
        },
        eventClick: (info) => {
          const taskId = parseInt(info.event.id);
          const task = this.tasks.find(t => t.id === taskId);
          if (task) {
            this.selectedTask = task;
            document.querySelectorAll('#calendar .fc-event.is-selected').forEach(n => n.classList.remove('is-selected'));
            info.el.classList.add('is-selected');
          }
        }
      });
      _raw.calendar.render();
      this.updateCalendarEvents();
    },

    updateCalendarEvents() {
      if (!_raw.calendar) return;
      const events = this.tasks.map(t => ({
        id: String(t.id),
        title: `${t.subject_name}: ${t.topic_name}`,
        start: `${t.date}T${t.start_time}:00`,
        end: `${t.date}T${t.end_time}:00`,
        classNames: [`ev-${t.status}`, `ev-${t.type}`],
        extendedProps: {
          color: t.color || '#3A5BA0',
          subject: t.subject_name,
          topic: t.topic_name,
          type: t.type,
          status: t.status
        }
      }));
      _raw.calendar.removeAllEvents();
      _raw.calendar.addEventSource(events);
      this.scrollToEarliestSession();
    },

    scrollToEarliestSession() {
      if (!_raw.calendar) return;
      let targetTime = '17:30:00';
      if (this.tasks && this.tasks.length > 0) {
        const startTimes = this.tasks
          .map(t => t.start_time)
          .filter(Boolean)
          .sort();
        if (startTimes.length > 0) {
          let earliest = startTimes[0];
          if (earliest.length === 5) earliest += ':00';
          targetTime = earliest;
        }
      }
      _raw.calendar.setOption('scrollTime', targetTime);
      setTimeout(() => {
        if (_raw.calendar && typeof _raw.calendar.scrollToTime === 'function') {
          _raw.calendar.scrollToTime(targetTime);
        }
      }, 50);
    },

    // ----------------- Chart.js Visualizations -----------------

    initCharts() {
      Chart.defaults.font.family = "'Inter', ui-sans-serif, system-ui, sans-serif";
      Chart.defaults.font.size = 12;

      const typeCtx = document.getElementById('taskTypeChart');
      if (typeCtx) {
        _raw.taskTypeChart = new Chart(typeCtx, {
          type: 'doughnut',
          data: {
            labels: ['Study', 'Revision', 'Quiz'],
            datasets: [{ data: [0, 0, 0], borderWidth: 3, hoverOffset: 4 }]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            cutout: '70%',
            plugins: { legend: { position: 'bottom', labels: { usePointStyle: true, pointStyle: 'circle', boxWidth: 8, padding: 16 } } }
          }
        });
      }

      const hoursCtx = document.getElementById('dailyHoursChart');
      if (hoursCtx) {
        _raw.dailyHoursChart = new Chart(hoursCtx, {
          type: 'bar',
          data: { labels: [], datasets: [{ label: 'Study hours', data: [], borderRadius: 6, borderSkipped: false, maxBarThickness: 28 }] },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
              x: { grid: { display: false }, border: { display: false } },
              y: { beginAtZero: true, border: { display: false }, ticks: { precision: 0, callback: v => v + 'h' } }
            },
            plugins: { legend: { display: false } }
          }
        });
      }

      // Chart.js starts its plugins asynchronously after attaching to the DOM.
      setTimeout(() => { this.applyChartTheme(); this.updateCharts(); }, 80);
    },

    // Chart.js can only update() once attached to the DOM; before that the mutated
    // config is simply picked up when the chart starts.
    refreshChart(chart) {
      if (chart && chart.attached && chart.legend) chart.update();
    },

    applyChartTheme() {
      // Text + grid colours come from Chart.defaults (resolved lazily), so we never
      // have to write into a live chart's resolved options.
      Chart.defaults.color = this.cssVar('--muted');
      Chart.defaults.borderColor = this.cssVar('--line');

      if (_raw.taskTypeChart) {
        const ds = _raw.taskTypeChart.data.datasets[0];
        ds.backgroundColor = [this.cssVar('--chart-1'), this.cssVar('--chart-2'), this.cssVar('--chart-3')];
        ds.borderColor = this.cssVar('--surface');
        this.refreshChart(_raw.taskTypeChart);
      }
      if (_raw.dailyHoursChart) {
        _raw.dailyHoursChart.data.datasets[0].backgroundColor = this.cssVar('--chart-2');
        this.refreshChart(_raw.dailyHoursChart);
      }
    },

    updateCharts() {
      if (_raw.taskTypeChart && this.dashboard.task_types) {
        const types = this.dashboard.task_types;
        _raw.taskTypeChart.data.datasets[0].data = [types.study || 0, types.revision || 0, types.quiz || 0];
        this.refreshChart(_raw.taskTypeChart);
      }

      if (_raw.dailyHoursChart && this.dashboard.daily_hours) {
        const daily = this.dashboard.daily_hours;
        const hours = daily.map(d => d.hours);
        const cap = Number(this.setupData?.profile?.max_daily_hours) || 4;
        _raw.dailyHoursChart.data.labels = daily.map(d => d.date.slice(5));
        _raw.dailyHoursChart.data.datasets[0].data = hours;
        // Don't clip days above the old fixed max of 6h; always show headroom up to the daily cap.
        _raw.dailyHoursChart.options.scales.y.suggestedMax = Math.max(cap, ...hours, 1);
        this.refreshChart(_raw.dailyHoursChart);
      }
    },

    // ----------------- Task Progress & Explanations -----------------

    async setTaskStatus(taskId, status) {
      // Optimistic update so UI immediately reflects "missed", "done", or "review"
      const prevTask = (this.tasks || []).find(t => t.id === taskId);
      const prevStatus = prevTask ? prevTask.status : null;
      if (prevTask) prevTask.status = status;
      const tTask = (this.todayTasks || []).find(t => t.id === taskId);
      if (tTask) tTask.status = status;
      if (this.selectedTask && this.selectedTask.id === taskId) {
        this.selectedTask.status = status;
      }
      if (this.dashboard && this.dashboard.summary) {
        if (prevStatus !== 'missed' && status === 'missed') {
          this.dashboard.summary.missed_tasks = (this.dashboard.summary.missed_tasks || 0) + 1;
          if (prevStatus === 'pending') {
            this.dashboard.summary.pending_tasks = Math.max(0, (this.dashboard.summary.pending_tasks || 1) - 1);
          }
        } else if (prevStatus === 'missed' && status !== 'missed') {
          this.dashboard.summary.missed_tasks = Math.max(0, (this.dashboard.summary.missed_tasks || 1) - 1);
        }
        if (prevStatus !== 'done' && status === 'done') {
          this.dashboard.summary.done_tasks = (this.dashboard.summary.done_tasks || 0) + 1;
          if (prevStatus === 'pending') {
            this.dashboard.summary.pending_tasks = Math.max(0, (this.dashboard.summary.pending_tasks || 1) - 1);
          }
        } else if (prevStatus === 'done' && status !== 'done') {
          this.dashboard.summary.done_tasks = Math.max(0, (this.dashboard.summary.done_tasks || 1) - 1);
        }
      }

      try {
        const res = await fetch(`/api/tasks/${taskId}/status`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status })
        });
        if (res.ok) {
          const data = await res.json();
          if (data.auto_replanned && data.replan_details) {
            this.replanDelta = data.replan_details;
            this.toast('Schedule rebalanced around your missed session.', 'info');
          }
          await this.loadAllData();
          if (this.selectedTask && this.selectedTask.id === taskId) {
            this.selectedTask.status = status;
          }
        } else {
          // Revert on error
          if (prevTask) prevTask.status = prevStatus;
          if (tTask) tTask.status = prevStatus;
          this.toast(await this.apiError(res, 'Could not update the session.'), 'error');
        }
      } catch (e) {
        if (prevTask) prevTask.status = prevStatus;
        if (tTask) tTask.status = prevStatus;
        console.error('Failed to update task status:', e);
        this.toast('Could not reach the server. Please try again.', 'error');
      }
    },

    async openExplainModal(taskId) {
      try {
        const res = await fetch(`/api/tasks/${taskId}/explain`);
        if (res.ok) {
          this.explanationData = await res.json();
          this.explainModalOpen = true;
        }
      } catch (e) {
        console.error('Explain fetch error:', e);
        this.toast('Could not reach the server. Please try again.', 'error');
      }
    },

    // ----------------- Autonomous Planner Live Streaming -----------------

    async triggerGeneratePlan() {
      if (this.agentWorking) return;
      if (!this.hasData) {
        this.promptNewPlan();
        return;
      }
      this.agentWorking = true;
      this.agentEvents = [];
      this.lastRunFallback = false;
      this.fallbackReason = '';
      this.switchTab('agent');

      try {
        const res = await fetch('/api/plan/generate', { method: 'POST' });
        if (!res.ok) {
          const err = await res.json();
          throw new Error(err.detail || 'Failed to initiate agent');
        }
        const data = await res.json();
        this.activeRunId = data.run_id;

        // Connect to SSE stream
        this.connectAgentStream(this.activeRunId);
      } catch (e) {
        console.error('Planner run error:', e);
        this.toast(e.message, 'error');
        this.agentWorking = false;
      }
    },

    connectAgentStream(runId) {
      if (this.eventSource) this.eventSource.close();

      this.eventSource = new EventSource(`/api/agent/stream/${runId}`);

      this.eventSource.onmessage = (e) => {
        try {
          const parsed = JSON.parse(e.data);
          this.agentEvents.push(parsed);

          if (parsed.type === 'start') {
            this.lastRunProvider = parsed.data?.provider || '';
          }

          if (parsed.type === 'fallback') {
            this.lastRunFallback = true;
            this.fallbackReason = parsed.data?.message || 'Gemini unavailable, used Mock for this run';
            this.toast(this.fallbackReason, 'warn');
          }

          if (parsed.type === 'done' || parsed.type === 'error') {
            if (parsed.data?.provider) this.lastRunProvider = parsed.data.provider;
            if (parsed.data?.is_fallback) {
              this.lastRunFallback = true;
              this.fallbackReason = 'Gemini unavailable, used Mock for this run';
            }
            this.eventSource.close();
            this.agentWorking = false;
            this.loadAllData();
            this.fetchAgentRuns();
          }
        } catch (err) {
          // Heartbeat or parse skip
        }
      };

      this.eventSource.onerror = () => {
        this.eventSource.close();
        this.agentWorking = false;
        this.loadAllData();
        this.fetchAgentRuns();
      };
    },

    async fetchAgentRuns() {
      try {
        const res = await fetch('/api/agent/runs');
        if (res.ok) {
          const data = await res.json();
          this.agentRuns = data.runs || [];
        }
      } catch (e) {
        console.error('Failed to fetch agent runs:', e);
      }
    },

    async loadAgentRun(runId) {
      if (!runId) return;
      try {
        const res = await fetch(`/api/agent/runs/${runId}/events`);
        if (res.ok) {
          const data = await res.json();
          this.agentEvents = data.events || [];
          this.activeRunId = runId;
          this.viewingHistoryRunId = runId;
          const runMeta = (this.agentRuns || []).find(r => r.id === runId);
          if (runMeta) {
            this.lastRunProvider = runMeta.provider || '';
            this.lastRunFallback = Boolean(runMeta.is_fallback);
          }
        }
      } catch (e) {
        console.error('Failed to load agent run events:', e);
        this.toast('Could not load past run events', 'error');
      }
    },

    async triggerManualReplan() {
      const reason = await this.askInput({
        title: 'Replan your schedule',
        message: 'Unfinished and missed sessions will be redistributed across the remaining days.',
        label: 'Reason',
        value: 'Adjusting study hours for upcoming week',
        confirmLabel: 'Replan'
      });
      if (!reason || !reason.trim()) return;

      try {
        const res = await fetch('/api/plan/replan', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ from_date: this.todayStr, reason })
        });
        if (res.ok) {
          this.replanDelta = await res.json();
          await this.loadAllData();
          this.toast('Schedule successfully redistributed!', 'success');
        }
      } catch (e) {
        console.error('Replan error:', e);
        this.toast('Could not reach the server. Please try again.', 'error');
      }
    },

    async triggerPanicMode(subjectId, subjectName) {
      const daysStr = await this.askInput({
        title: `Panic mode · ${subjectName}`,
        message: 'Low-yield topics are dropped and high-weightage topics are kept so you can cover what matters most.',
        label: 'Days left until the exam',
        value: '4',
        type: 'number',
        min: 1,
        max: 60,
        confirmLabel: 'Activate'
      });
      if (!daysStr) return;
      const daysLeft = parseInt(daysStr);
      if (!daysLeft || daysLeft < 1) {
        this.toast('Enter a valid number of days (1 or more).', 'warn');
        return;
      }

      try {
        const res = await fetch('/api/plan/panic', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ subject_id: subjectId, days_left: daysLeft })
        });
        if (res.ok) {
          const data = await res.json();
          this.toast(`Panic Mode Activated!\n${data.explanation}\nKept: ${data.kept_count} topics | Dropped: ${data.dropped_count} low-yield topics.`, 'success');
          await this.loadAllData();
        }
      } catch (e) {
        console.error('Panic mode error:', e);
        this.toast('Could not reach the server. Please try again.', 'error');
      }
    },

    // ----------------- Quiz Flow -----------------

    startTopicQuiz(topicId) {
      this.selectedQuizTopicId = String(topicId);
      this.switchTab('quiz');
      this.loadTopicQuiz(topicId);
    },

    async loadTopicQuiz(topicId) {
      if (!topicId) return;
      this.quizLoading = true;
      this.activeQuiz = null;
      this.quizSubmitted = false;
      this.userAnswers = [];
      this.quizResult = null;

      try {
        const res = await fetch('/api/quiz/generate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ topic_id: parseInt(topicId), n: 3 })
        });
        if (res.ok) {
          this.activeQuiz = await res.json();
          this.userAnswers = new Array(this.activeQuiz.questions.length).fill('');
        }
      } catch (e) {
        console.error('Quiz generation error:', e);
        this.toast('Could not reach the server. Please try again.', 'error');
      } finally {
        this.quizLoading = false;
      }
    },

    async submitCurrentQuiz() {
      if (!this.activeQuiz) return;
      try {
        const res = await fetch('/api/quiz/submit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            topic_id: this.activeQuiz.topic_id,
            answers: this.userAnswers,
            questions: this.activeQuiz.questions
          })
        });
        if (res.ok) {
          this.quizResult = await res.json();
          this.quizSubmitted = true;
          window.scrollTo({ top: 0, behavior: 'smooth' });
          await this.loadAllData();
        }
      } catch (e) {
        console.error('Quiz submit error:', e);
        this.toast('Could not reach the server. Please try again.', 'error');
      }
    },

    // ----------------- Syllabus & Profile Setup -----------------

    async submitSyllabus() {
      const fileInput = document.getElementById('pdfFileInput');
      const formData = new FormData();

      if (fileInput && fileInput.files[0]) {
        formData.append('file', fileInput.files[0]);
      } else if (this.syllabusText.trim()) {
        formData.append('text', this.syllabusText.trim());
      } else {
        this.toast('Please paste syllabus text or choose a PDF file.', 'warn');
        return;
      }

      try {
        const res = await fetch('/api/syllabus', {
          method: 'POST',
          body: formData
        });
        if (res.ok) {
          const data = await res.json();
          this.toast(`Syllabus parsed!\nAdded ${data.subjects_added} subject(s) and ${data.topics_added} topics.`, 'success');
          this.syllabusText = '';
          if (fileInput) fileInput.value = '';
          await this.loadAllData();
          await this.fetchStatus();
        }
      } catch (e) {
        console.error('Syllabus upload error:', e);
        this.toast('Could not reach the server. Please try again.', 'error');
      }
    },

    async saveProfileSettings() {
      try {
        const res = await fetch('/api/profile', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.setupData.profile)
        });
        if (res.ok) {
          this.toast('Study preferences and guardrails saved!', 'success');
          await this.loadAllData();
        }
      } catch (e) {
        console.error('Save profile error:', e);
        this.toast('Could not reach the server. Please try again.', 'error');
      }
    },

    async reloadDemoSeed() {
      if (this.hasData && !(await this.askConfirm({
        title: 'Load the demo student?',
        message: 'Sample engineering data with fresh future exam dates will replace your current plan.',
        confirmLabel: 'Replace with demo',
        danger: true
      }))) {
        return;
      }
      try {
        const res = await fetch('/api/seed', { method: 'POST' });
        if (res.ok) {
          await this.loadAllData();
          await this.fetchStatus();
          this.toast('Demo student loaded with fresh future exam dates and 16 syllabus topics!', 'success');
        }
      } catch (e) {
        console.error('Reload demo seed error:', e);
        this.toast('Could not reach the server. Please try again.', 'error');
      }
    }

  };
}
