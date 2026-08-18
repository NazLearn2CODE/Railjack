import { useEffect, useState, useCallback } from "react";
import {
  Calendar as CalendarIcon,
  ChevronLeft,
  ChevronRight,
  Plus,
  CheckCircle2,
  Circle,
  Copy,
  Terminal,
  Trash2,
  Tag,
  FolderGit2,
  RotateCw,
  Clock,
  Sparkles,
  Check,
  X,
  Code2,
} from "lucide-react";
import {
  fetchMonthOverview,
  fetchDayTasks,
  createCalendarTask,
  updateCalendarTaskStatus,
  deleteCalendarTask,
  dispatchCalendarPrompt,
  type MonthOverview,
  type CalendarTask,
} from "../api";

export default function CalendarPanel() {
  const today = new Date();
  const [currentYear, setCurrentYear] = useState<number>(today.getFullYear());
  const [currentMonth, setCurrentMonth] = useState<number>(today.getMonth() + 1);
  const [selectedDate, setSelectedDate] = useState<string>(
    today.toISOString().split("T")[0]
  );

  const [monthData, setMonthData] = useState<MonthOverview | null>(null);
  const [dayTasks, setDayTasks] = useState<CalendarTask[]>([]);
  const [formattedDate, setFormattedDate] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  // Quick Add Modal state
  const [showAddModal, setShowAddModal] = useState<boolean>(false);
  const [addType, setAddType] = useState<"reminder" | "prompt_task">("reminder");
  const [addTitle, setAddTitle] = useState<string>("");
  const [addDate, setAddDate] = useState<string>(selectedDate);
  const [addTags, setAddTags] = useState<string>("");
  const [addTargetRepo, setAddTargetRepo] = useState<string>("");
  const [addPrompt, setAddPrompt] = useState<string>("");
  const [addRecurrence, setAddRecurrence] = useState<string>("none");
  const [submitting, setSubmitting] = useState<boolean>(false);

  // Visual feedback states
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [dispatchedId, setDispatchedId] = useState<string | null>(null);

  const loadMonth = useCallback(async (y: number, m: number) => {
    try {
      const data = await fetchMonthOverview(y, m);
      setMonthData(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const loadDay = useCallback(async (d: string) => {
    setLoading(true);
    try {
      const data = await fetchDayTasks(d);
      setDayTasks(data.tasks);
      setFormattedDate(data.date_formatted);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadMonth(currentYear, currentMonth);
  }, [currentYear, currentMonth, loadMonth]);

  useEffect(() => {
    loadDay(selectedDate);
  }, [selectedDate, loadDay]);

  const handlePrevMonth = () => {
    if (currentMonth === 1) {
      setCurrentMonth(12);
      setCurrentYear(currentYear - 1);
    } else {
      setCurrentMonth(currentMonth - 1);
    }
  };

  const handleNextMonth = () => {
    if (currentMonth === 12) {
      setCurrentMonth(1);
      setCurrentYear(currentYear + 1);
    } else {
      setCurrentMonth(currentMonth + 1);
    }
  };

  const handleJumpToday = () => {
    const now = new Date();
    setCurrentYear(now.getFullYear());
    setCurrentMonth(now.getMonth() + 1);
    const todayStr = now.toISOString().split("T")[0];
    setSelectedDate(todayStr);
  };

  const handleToggleStatus = async (task: CalendarTask) => {
    const nextStatus = task.status === "completed" ? "pending" : "completed";
    try {
      await updateCalendarTaskStatus(task.id, nextStatus);
      await loadDay(selectedDate);
      await loadMonth(currentYear, currentMonth);
    } catch (e) {
      alert("Failed to update status: " + (e instanceof Error ? e.message : String(e)));
    }
  };

  const handleDelete = async (taskId: string) => {
    if (!confirm("Delete this calendar entry?")) return;
    try {
      await deleteCalendarTask(taskId);
      await loadDay(selectedDate);
      await loadMonth(currentYear, currentMonth);
    } catch (e) {
      alert("Failed to delete task: " + (e instanceof Error ? e.message : String(e)));
    }
  };

  const handleCopyPrompt = (task: CalendarTask) => {
    const text = task.interpolated_prompt || task.prompt || "";
    navigator.clipboard.writeText(text);
    setCopiedId(task.id);
    setTimeout(() => setCopiedId(null), 2000);
  };

  const handleDispatchPrompt = async (task: CalendarTask) => {
    try {
      const res = await dispatchCalendarPrompt(task.id, selectedDate);
      if (res.status === "ok") {
        setDispatchedId(task.id);
        setTimeout(() => setDispatchedId(null), 2500);
      } else {
        alert("Dispatch error: " + (res.detail || "Unknown error"));
      }
    } catch (e) {
      alert("Failed to dispatch prompt: " + (e instanceof Error ? e.message : String(e)));
    }
  };

  const handleCreateTask = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!addTitle.trim()) return;

    setSubmitting(true);
    try {
      let cronValue: string | undefined = undefined;
      if (addRecurrence === "daily") cronValue = "daily";
      else if (addRecurrence === "monthly") {
        const dayNum = parseInt(addDate.split("-")[2], 10);
        cronValue = `monthly:${dayNum}`;
      } else if (addRecurrence === "weekly") {
        const dayOfWeek = new Date(addDate).getDay(); // 0=Sun
        cronValue = `weekly:${dayOfWeek}`;
      }

      const tagsList = addTags
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean);

      await createCalendarTask({
        date: cronValue ? undefined : addDate,
        type: addType,
        title: addTitle.trim(),
        tags: tagsList,
        target_repo: addTargetRepo.trim() || undefined,
        prompt: addPrompt.trim() || undefined,
        cron: cronValue,
      });

      setShowAddModal(false);
      setAddTitle("");
      setAddTags("");
      setAddPrompt("");
      setAddTargetRepo("");
      setAddRecurrence("none");

      await loadDay(selectedDate);
      await loadMonth(currentYear, currentMonth);
    } catch (err) {
      alert("Failed to create task: " + (err instanceof Error ? err.message : String(err)));
    } finally {
      setSubmitting(false);
    }
  };

  const insertToken = (token: string) => {
    setAddPrompt((prev) => prev + token);
  };

  // Calendar Grid Calculation
  const firstDayOfMonth = new Date(currentYear, currentMonth - 1, 1);
  const startingDayOfWeek = (firstDayOfMonth.getDay() + 6) % 7; // 0=Monday
  const daysInMonth = new Date(currentYear, currentMonth, 0).getDate();
  const todayStr = today.toISOString().split("T")[0];

  const calendarDays: Array<{ dateStr: string; dayNum: number; isCurrentMonth: boolean }> = [];

  // Previous month padding
  const prevMonthDays = new Date(currentYear, currentMonth - 1, 0).getDate();
  for (let i = startingDayOfWeek - 1; i >= 0; i--) {
    const dNum = prevMonthDays - i;
    const prevMonth = currentMonth === 1 ? 12 : currentMonth - 1;
    const prevYear = currentMonth === 1 ? currentYear - 1 : currentYear;
    const dStr = `${prevYear}-${String(prevMonth).padStart(2, "0")}-${String(dNum).padStart(2, "0")}`;
    calendarDays.push({ dateStr: dStr, dayNum: dNum, isCurrentMonth: false });
  }

  // Current month days
  for (let d = 1; d <= daysInMonth; d++) {
    const dStr = `${currentYear}-${String(currentMonth).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    calendarDays.push({ dateStr: dStr, dayNum: d, isCurrentMonth: true });
  }

  // Next month padding
  const remainingCells = 42 - calendarDays.length;
  for (let d = 1; d <= remainingCells; d++) {
    const nextMonth = currentMonth === 12 ? 1 : currentMonth + 1;
    const nextYear = currentMonth === 12 ? currentYear + 1 : currentYear;
    const dStr = `${nextYear}-${String(nextMonth).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    calendarDays.push({ dateStr: dStr, dayNum: d, isCurrentMonth: false });
  }

  return (
    <div className="flex flex-col h-full w-full select-none overflow-hidden text-neutral-200">
      {/* Top Header Bar */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-white/10 bg-black/40 backdrop-blur-md">
        <div className="flex items-center gap-3">
          <div className="p-1.5 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-400">
            <CalendarIcon className="w-5 h-5" />
          </div>
          <div>
            <h1 className="text-sm font-semibold tracking-wider uppercase text-amber-300 flex items-center gap-2">
              Working Calendar & Prompt Launcher
              <span className="text-[10px] px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 font-mono">
                ● Cephalon Vault Sync
              </span>
            </h1>
            <p className="text-xs text-neutral-400">
              Personal instruction scheduler • 1-click prompt dispatch to terminal
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={() => {
              setAddDate(selectedDate);
              setShowAddModal(true);
            }}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gradient-to-r from-amber-500 to-amber-600 hover:from-amber-400 hover:to-amber-500 text-black font-medium text-xs transition-all shadow-lg shadow-amber-500/10"
          >
            <Plus className="w-4 h-4" />
            <span>New Task</span>
          </button>
        </div>
      </div>

      {error && (
        <div className="px-4 py-2 bg-red-500/10 border-b border-red-500/30 text-red-300 text-xs font-mono flex items-center justify-between">
          <span>Error loading calendar: {error}</span>
          <button onClick={() => setError(null)} className="text-red-400 hover:text-white">✕</button>
        </div>
      )}

      {/* Main 2-Column Split Layout */}
      <div className="flex flex-1 min-h-0 divide-x divide-white/10 overflow-hidden">
        {/* Left Column: Month Overview Grid */}
        <div className="w-[380px] shrink-0 flex flex-col bg-black/20 p-4 overflow-y-auto">
          {/* Month Navigator */}
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-base font-bold text-neutral-100 tracking-wide">
              {monthData?.month_name || "Month"} {currentYear}
            </h2>
            <div className="flex items-center gap-1">
              <button
                onClick={handleJumpToday}
                className="px-2 py-1 text-xs rounded bg-white/5 hover:bg-white/10 border border-white/10 text-neutral-300 font-mono"
              >
                Today
              </button>
              <button
                onClick={handlePrevMonth}
                className="p-1 rounded hover:bg-white/10 text-neutral-400 hover:text-white"
                title="Previous Month"
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
              <button
                onClick={handleNextMonth}
                className="p-1 rounded hover:bg-white/10 text-neutral-400 hover:text-white"
                title="Next Month"
              >
                <ChevronRight className="w-4 h-4" />
              </button>
            </div>
          </div>

          {/* Weekday Headers */}
          <div className="grid grid-cols-7 gap-1 text-center text-[11px] font-semibold text-neutral-400 mb-1 font-mono">
            <span>MON</span>
            <span>TUE</span>
            <span>WED</span>
            <span>THU</span>
            <span>FRI</span>
            <span>SAT</span>
            <span>SUN</span>
          </div>

          {/* Days Grid */}
          <div className="grid grid-cols-7 gap-1">
            {calendarDays.map((cell) => {
              const summary = monthData?.days[cell.dateStr];
              const isSelected = selectedDate === cell.dateStr;
              const isToday = todayStr === cell.dateStr;

              return (
                <button
                  key={cell.dateStr}
                  onClick={() => setSelectedDate(cell.dateStr)}
                  className={`relative flex flex-col items-center justify-between p-1.5 h-14 rounded-lg transition-all border ${
                    isSelected
                      ? "bg-amber-500/20 border-amber-400 text-white font-bold shadow-lg shadow-amber-500/10"
                      : isToday
                      ? "bg-white/10 border-amber-500/60 text-amber-300"
                      : cell.isCurrentMonth
                      ? "bg-white/[0.03] border-white/5 hover:border-white/20 text-neutral-300 hover:bg-white/[0.06]"
                      : "bg-transparent border-transparent text-neutral-600 hover:bg-white/[0.02]"
                  }`}
                >
                  <span className="text-xs font-mono">{cell.dayNum}</span>

                  {/* Badges / Status Dots */}
                  {summary && (
                    <div className="flex items-center gap-1 mt-0.5">
                      {summary.prompts > 0 && (
                        <span
                          className="w-1.5 h-1.5 rounded-full bg-violet-400 ring-2 ring-violet-500/30"
                          title={`${summary.prompts} Prompt Task(s)`}
                        />
                      )}
                      {summary.reminders > 0 && (
                        <span
                          className="w-1.5 h-1.5 rounded-full bg-cyan-400 ring-2 ring-cyan-500/30"
                          title={`${summary.reminders} Reminder(s)`}
                        />
                      )}
                      {summary.completed > 0 && (
                        <span
                          className="w-1.5 h-1.5 rounded-full bg-emerald-400 ring-2 ring-emerald-500/30"
                          title={`${summary.completed} Completed`}
                        />
                      )}
                    </div>
                  )}
                </button>
              );
            })}
          </div>

          {/* Legend */}
          <div className="flex items-center justify-around mt-6 pt-3 border-t border-white/10 text-[11px] text-neutral-400 font-mono">
            <span className="flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-cyan-400" /> Reminders
            </span>
            <span className="flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-violet-400" /> Prompt Tasks
            </span>
            <span className="flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-emerald-400" /> Done
            </span>
          </div>
        </div>

        {/* Right Column: Selected Day Agenda & Prompt Launcher */}
        <div className="flex-1 flex flex-col bg-black/40 p-6 overflow-y-auto">
          {/* Day Header */}
          <div className="flex items-center justify-between pb-4 mb-4 border-b border-white/10">
            <div>
              <h2 className="text-xl font-bold text-neutral-100 flex items-center gap-2">
                {formattedDate || selectedDate}
                {todayStr === selectedDate && (
                  <span className="text-[10px] px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/40 uppercase font-mono">
                    Today
                  </span>
                )}
              </h2>
              <p className="text-xs text-neutral-400 mt-0.5">
                {dayTasks.length} task{dayTasks.length === 1 ? "" : "s"} scheduled
              </p>
            </div>
            <button
              onClick={() => loadDay(selectedDate)}
              className="p-1.5 rounded-lg hover:bg-white/10 text-neutral-400 hover:text-white"
              title="Refresh"
            >
              <RotateCw className="w-4 h-4" />
            </button>
          </div>

          {/* Task Feed */}
          {loading ? (
            <div className="flex flex-col items-center justify-center py-20 text-neutral-500">
              <RotateCw className="w-6 h-6 animate-spin mb-2 text-amber-400" />
              <span className="text-xs font-mono">Loading scheduled tasks...</span>
            </div>
          ) : dayTasks.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-20 border border-dashed border-white/10 rounded-2xl p-8 text-center">
              <div className="p-3 rounded-full bg-white/5 text-neutral-500 mb-3">
                <CalendarIcon className="w-8 h-8" />
              </div>
              <h3 className="text-sm font-semibold text-neutral-300 mb-1">
                No Tasks Scheduled for This Date
              </h3>
              <p className="text-xs text-neutral-500 max-w-sm mb-4">
                Keep on top of your workflow with prompt-ready instruction sets or simple reminders.
              </p>
              <button
                onClick={() => {
                  setAddDate(selectedDate);
                  setShowAddModal(true);
                }}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-white/10 hover:bg-white/15 text-neutral-200 text-xs transition-all border border-white/10 font-medium"
              >
                <Plus className="w-4 h-4" />
                <span>Add Task to {selectedDate}</span>
              </button>
            </div>
          ) : (
            <div className="space-y-4">
              {dayTasks.map((task) => {
                const isCompleted = task.status === "completed";
                const isPrompt = task.type === "prompt_task";

                return (
                  <div
                    key={task.id}
                    className={`rounded-xl border transition-all ${
                      isCompleted
                        ? "bg-black/30 border-white/5 opacity-60"
                        : isPrompt
                        ? "bg-violet-950/20 border-violet-500/30 hover:border-violet-500/50 shadow-lg shadow-violet-950/20"
                        : "bg-white/[0.03] border-white/10 hover:border-white/20"
                    } p-4`}
                  >
                    {/* Top Row: Checkbox, Title, Badges, Delete */}
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex items-start gap-3 flex-1">
                        <button
                          onClick={() => handleToggleStatus(task)}
                          className="mt-0.5 text-neutral-400 hover:text-amber-400 transition-colors"
                        >
                          {isCompleted ? (
                            <CheckCircle2 className="w-5 h-5 text-emerald-400" />
                          ) : (
                            <Circle className="w-5 h-5" />
                          )}
                        </button>
                        <div>
                          <div className="flex items-center gap-2 flex-wrap">
                            <span
                              className={`text-sm font-semibold ${
                                isCompleted
                                  ? "line-through text-neutral-500"
                                  : "text-neutral-100"
                              }`}
                            >
                              {task.title}
                            </span>
                            {isPrompt && (
                              <span className="text-[10px] px-2 py-0.5 rounded bg-violet-500/20 text-violet-300 border border-violet-500/40 font-mono flex items-center gap-1">
                                <Sparkles className="w-3 h-3" /> Prompt Task
                              </span>
                            )}
                            {task.is_recurring && (
                              <span className="text-[10px] px-2 py-0.5 rounded bg-blue-500/20 text-blue-300 border border-blue-500/40 font-mono flex items-center gap-1">
                                <Clock className="w-3 h-3" /> Recurring
                              </span>
                            )}
                            {task.target_repo && (
                              <span className="text-[10px] px-2 py-0.5 rounded bg-white/5 text-neutral-300 border border-white/10 font-mono flex items-center gap-1">
                                <FolderGit2 className="w-3 h-3 text-amber-400" />
                                {task.target_repo}
                              </span>
                            )}
                          </div>

                          {/* Tag chips */}
                          {task.tags && task.tags.length > 0 && (
                            <div className="flex items-center gap-1.5 mt-1.5 flex-wrap">
                              {task.tags.map((tag) => (
                                <span
                                  key={tag}
                                  className="text-[10px] px-1.5 py-0.5 rounded bg-white/5 text-neutral-400 border border-white/5 font-mono flex items-center gap-1"
                                >
                                  <Tag className="w-2.5 h-2.5" />
                                  {tag}
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      </div>

                      {/* Delete */}
                      <button
                        onClick={() => handleDelete(task.id)}
                        className="p-1.5 rounded hover:bg-red-500/20 text-neutral-500 hover:text-red-400 transition-colors"
                        title="Delete task"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>

                    {/* Prompt Box & Action Row for Prompt Tasks */}
                    {isPrompt && (task.interpolated_prompt || task.prompt) && (
                      <div className="mt-3 pt-3 border-t border-white/10 space-y-3">
                        <div className="relative rounded-lg bg-black/60 border border-white/10 p-3 font-mono text-xs text-neutral-300 max-h-48 overflow-y-auto whitespace-pre-wrap leading-relaxed select-text">
                          <div className="flex items-center justify-between text-[10px] text-neutral-500 uppercase pb-1 mb-2 border-b border-white/5 font-semibold">
                            <span className="flex items-center gap-1 text-violet-400">
                              <Code2 className="w-3.5 h-3.5" /> Prompt Instructions (Tokens Interpolated)
                            </span>
                          </div>
                          {task.interpolated_prompt || task.prompt}
                        </div>

                        {/* Action Buttons */}
                        <div className="flex items-center justify-end gap-2">
                          <button
                            onClick={() => handleCopyPrompt(task)}
                            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-white/10 hover:bg-white/15 text-neutral-200 text-xs font-medium transition-all border border-white/10"
                          >
                            {copiedId === task.id ? (
                              <>
                                <Check className="w-3.5 h-3.5 text-emerald-400" />
                                <span className="text-emerald-400 font-mono">Copied!</span>
                              </>
                            ) : (
                              <>
                                <Copy className="w-3.5 h-3.5" />
                                <span>Copy Prompt</span>
                              </>
                            )}
                          </button>

                          <button
                            onClick={() => handleDispatchPrompt(task)}
                            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gradient-to-r from-violet-600 to-indigo-600 hover:from-violet-500 hover:to-indigo-500 text-white text-xs font-medium transition-all shadow-lg shadow-violet-600/20"
                          >
                            {dispatchedId === task.id ? (
                              <>
                                <Check className="w-3.5 h-3.5 text-emerald-300" />
                                <span className="font-mono">Injected to Terminal!</span>
                              </>
                            ) : (
                              <>
                                <Terminal className="w-3.5 h-3.5" />
                                <span>Insert to Terminal</span>
                              </>
                            )}
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {/* Quick Add Modal */}
      {showAddModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4">
          <div className="relative w-full max-w-lg bg-neutral-900 border border-white/15 rounded-2xl shadow-2xl p-6 overflow-hidden">
            <div className="flex items-center justify-between pb-3 border-b border-white/10 mb-4">
              <h3 className="text-base font-bold text-neutral-100 flex items-center gap-2">
                <Plus className="w-5 h-5 text-amber-400" /> Add Calendar Task
              </h3>
              <button
                onClick={() => setShowAddModal(false)}
                className="p-1 rounded-lg hover:bg-white/10 text-neutral-400 hover:text-white"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <form onSubmit={handleCreateTask} className="space-y-4">
              {/* Type Switcher */}
              <div className="grid grid-cols-2 gap-2 p-1 bg-black/40 rounded-lg border border-white/10 font-mono text-xs">
                <button
                  type="button"
                  onClick={() => setAddType("reminder")}
                  className={`py-1.5 rounded text-center transition-all ${
                    addType === "reminder"
                      ? "bg-cyan-500/20 text-cyan-300 font-bold border border-cyan-500/40"
                      : "text-neutral-400 hover:text-neutral-200"
                  }`}
                >
                  Reminder
                </button>
                <button
                  type="button"
                  onClick={() => setAddType("prompt_task")}
                  className={`py-1.5 rounded text-center transition-all ${
                    addType === "prompt_task"
                      ? "bg-violet-500/20 text-violet-300 font-bold border border-violet-500/40"
                      : "text-neutral-400 hover:text-neutral-200"
                  }`}
                >
                  Prompt Task
                </button>
              </div>

              {/* Title */}
              <div>
                <label className="block text-xs font-mono text-neutral-400 mb-1">
                  Task Title *
                </label>
                <input
                  type="text"
                  required
                  placeholder="e.g. Generate Monthly NEWSLINE Report"
                  value={addTitle}
                  onChange={(e) => setAddTitle(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg bg-black/50 border border-white/10 text-neutral-100 text-sm focus:outline-none focus:border-amber-400"
                />
              </div>

              {/* Date & Recurrence */}
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-mono text-neutral-400 mb-1">
                    Date
                  </label>
                  <input
                    type="date"
                    value={addDate}
                    onChange={(e) => setAddDate(e.target.value)}
                    className="w-full px-3 py-2 rounded-lg bg-black/50 border border-white/10 text-neutral-100 text-xs font-mono focus:outline-none focus:border-amber-400"
                  />
                </div>
                <div>
                  <label className="block text-xs font-mono text-neutral-400 mb-1">
                    Recurrence
                  </label>
                  <select
                    value={addRecurrence}
                    onChange={(e) => setAddRecurrence(e.target.value)}
                    className="w-full px-3 py-2 rounded-lg bg-black/50 border border-white/10 text-neutral-100 text-xs font-mono focus:outline-none focus:border-amber-400"
                  >
                    <option value="none">One-time (Dated)</option>
                    <option value="daily">Daily</option>
                    <option value="weekly">Weekly (on this weekday)</option>
                    <option value="monthly">Monthly (on this day of month)</option>
                  </select>
                </div>
              </div>

              {/* Tags & Target Repo */}
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-mono text-neutral-400 mb-1">
                    Tags (comma-separated)
                  </label>
                  <input
                    type="text"
                    placeholder="e.g. report, newsline"
                    value={addTags}
                    onChange={(e) => setAddTags(e.target.value)}
                    className="w-full px-3 py-2 rounded-lg bg-black/50 border border-white/10 text-neutral-100 text-xs focus:outline-none focus:border-amber-400"
                  />
                </div>
                {addType === "prompt_task" && (
                  <div>
                    <label className="block text-xs font-mono text-neutral-400 mb-1">
                      Target Repo Path
                    </label>
                    <input
                      type="text"
                      placeholder="~/Coding Projects/Railjack"
                      value={addTargetRepo}
                      onChange={(e) => setAddTargetRepo(e.target.value)}
                      className="w-full px-3 py-2 rounded-lg bg-black/50 border border-white/10 text-neutral-100 text-xs font-mono focus:outline-none focus:border-amber-400"
                    />
                  </div>
                )}
              </div>

              {/* Prompt Body & Helper Token Chips */}
              {addType === "prompt_task" && (
                <div className="space-y-1.5">
                  <div className="flex items-center justify-between">
                    <label className="text-xs font-mono text-neutral-400">
                      Prompt Template (Harness-Agnostic Envelope)
                    </label>
                    <div className="flex items-center gap-1">
                      {["{{TODAY}}", "{{MONTH_NAME}}", "{{YEAR}}", "{{TARGET_REPO}}"].map(
                        (tok) => (
                          <button
                            key={tok}
                            type="button"
                            onClick={() => insertToken(tok)}
                            className="text-[10px] px-1.5 py-0.5 rounded bg-white/5 hover:bg-white/10 text-amber-300 font-mono border border-white/10"
                          >
                            + {tok}
                          </button>
                        )
                      )}
                    </div>
                  </div>
                  <textarea
                    rows={6}
                    placeholder={`### GOAL: Generate NEWSLINE report for {{MONTH_NAME}} {{YEAR}}\n### GROUND TRUTH: app/newsline_reports.py\n### CONSTRAINTS: Ponytail rules apply; verify by running.\n### INSTRUCTIONS:\n1. Execute step 1.`}
                    value={addPrompt}
                    onChange={(e) => setAddPrompt(e.target.value)}
                    className="w-full px-3 py-2 rounded-lg bg-black/50 border border-white/10 text-neutral-200 font-mono text-xs focus:outline-none focus:border-amber-400"
                  />
                </div>
              )}

              {/* Submit Buttons */}
              <div className="flex items-center justify-end gap-2 pt-2 border-t border-white/10">
                <button
                  type="button"
                  onClick={() => setShowAddModal(false)}
                  className="px-4 py-2 rounded-lg bg-white/5 hover:bg-white/10 text-neutral-300 text-xs font-mono"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={submitting}
                  className="px-4 py-2 rounded-lg bg-gradient-to-r from-amber-500 to-amber-600 hover:from-amber-400 hover:to-amber-500 text-black font-semibold text-xs shadow-lg shadow-amber-500/20"
                >
                  {submitting ? "Saving..." : "Create Task"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
