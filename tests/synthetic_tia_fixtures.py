"""Synthetic TIA XML samples, materialized only in test temporary directories.

These small fixtures contain invented blocks and retain their original bytes.
No exported PLC project is needed by the unit tests.
"""
from pathlib import Path


SYNTHETIC_XML = {
    "prodiag/Dev_FB.xml": r"""<?xml version="1.0" encoding="utf-8"?>
<Document>
  <SW.Blocks.FB ID="0">
    <AttributeList>
      <Interface><Sections xmlns="http://www.siemens.com/automation/Openness/SW/Interface/v5">
  <Section Name="Input">
    <Member Name="IN" Datatype="Bool"><Comment><MultiLanguageText Lang="en-US">Device ready</MultiLanguageText></Comment></Member>
    <Member Name="Name" Datatype="String"><AttributeList><BooleanAttribute Name="ExternalAccessible" SystemDefined="true">false</BooleanAttribute></AttributeList></Member>
  </Section>
  <Section Name="Static">
    <Member Name="Arr" Datatype="Array[1..4] of Bool" />
    <Member Name="St" Datatype="Struct">
      <Member Name="A b" Datatype="Bool"><Comment><MultiLanguageText Lang="ru-RU">Датчик</MultiLanguageText></Comment></Member>
    </Member>
    <Member Name="Hidden" Datatype="Bool"><AttributeList><BooleanAttribute Name="ExternalAccessible" SystemDefined="true">false</BooleanAttribute></AttributeList></Member>
  </Section>
</Sections></Interface>
      <Name>Dev_FB</Name>
      <Supervisions><BlockTypeSupervisions xmlns="http://www.siemens.com/automation/Openness/SW/BlockTypeSupervisions/v3">
  <BlockTypeSupervision Number="1" Type="Operand">
    <SupervisedOperand Name="#IN" />
    <SupervisedStatus>false</SupervisedStatus>
    <DelayOperand Name="T#1s_500ms" />
    <Conditions>
      <Condition><ConditionOperand Number="1" Name="#Arr[2]" /><TriggeringStatus>true</TriggeringStatus></Condition>
      <Condition><ConditionOperand Number="2" Name="" /><TriggeringStatus>true</TriggeringStatus></Condition>
      <Condition><ConditionOperand Number="3" Name="" /><TriggeringStatus>true</TriggeringStatus></Condition>
    </Conditions>
    <CategoryNumber>1</CategoryNumber><SubCategory1Number>2</SubCategory1Number><SubCategory2Number>0</SubCategory2Number>
    <SpecificField>
      <AssociatedValues><AssociatedValue><AssociatedValueOperand Number="1" Name="#Name" /></AssociatedValue></AssociatedValues>
      <SpecificFieldText><MultiLanguageText Lang="en-US">@4%s@ not ready</MultiLanguageText></SpecificFieldText>
    </SpecificField>
  </BlockTypeSupervision>
  <BlockTypeSupervision Number="2" Type="MessageError">
    <SupervisedOperand Name="#St.&quot;A b&quot;" />
    <SupervisedStatus>true</SupervisedStatus>
    <DelayOperand Name="T#0ms" />
    <Conditions>
      <Condition><ConditionOperand Number="1" Name="&quot;Glob_DB&quot;.x[3]" /><TriggeringStatus>false</TriggeringStatus></Condition>
      <Condition><ConditionOperand Number="2" Name="TRUE" /><TriggeringStatus>true</TriggeringStatus></Condition>
    </Conditions>
    <CategoryNumber>2</CategoryNumber><SubCategory1Number>0</SubCategory1Number><SubCategory2Number>0</SubCategory2Number>
  </BlockTypeSupervision>
  <BlockTypeSupervision Number="3" Type="Operand">
    <SupervisedOperand Name="#Hidden" />
    <SupervisedStatus>true</SupervisedStatus>
    <DelayOperand Name="T#0ms" />
    <CategoryNumber>1</CategoryNumber><SubCategory1Number>0</SubCategory1Number><SubCategory2Number>0</SubCategory2Number>
  </BlockTypeSupervision>
</BlockTypeSupervisions></Supervisions>
    </AttributeList>
  </SW.Blocks.FB>
</Document>
""",
    "prodiag/Glob_DB.xml": r"""<?xml version="1.0" encoding="utf-8"?>
<Document>
  <SW.Blocks.GlobalDB ID="0">
    <AttributeList>
      <Interface><Sections xmlns="http://www.siemens.com/automation/Openness/SW/Interface/v5">
  <Section Name="Static">
    <Member Name="x" Datatype="Array[1..8] of Bool" />
    <Member Name="ok" Datatype="Bool"><Comment><MultiLanguageText Lang="en-US">Network OK</MultiLanguageText></Comment></Member>
  </Section>
</Sections></Interface>
      <Name>Glob_DB</Name>
    </AttributeList>
  </SW.Blocks.GlobalDB>
</Document>
""",
    "prodiag/Main_OB.xml": r"""<?xml version="1.0" encoding="utf-8"?>
<Document>
  <SW.Blocks.OB ID="0">
    <AttributeList><Name>Main</Name></AttributeList>
    <ObjectList>
      <SW.Blocks.CompileUnit ID="1" CompositionName="CompileUnits">
        <AttributeList>
          <NetworkSource><FlgNet xmlns="http://www.siemens.com/automation/Openness/SW/NetworkSource/FlgNet/v4">
  <Parts>
    <Call UId="21"><CallInfo Name="Parent_FB" BlockType="FB"><Instance Scope="GlobalVariable" UId="22"><Component Name="Parent_DB1" /></Instance></CallInfo></Call>
    <Call UId="23"><CallInfo Name="Parent_FB" BlockType="FB"><Instance Scope="GlobalVariable" UId="24"><Component Name="Parent_DB2" /></Instance></CallInfo></Call>
    <Call UId="25"><CallInfo Name="Dev_FB" BlockType="FB"><Instance Scope="GlobalVariable" UId="26"><Component Name="Dev_DB" /></Instance></CallInfo></Call>
  </Parts>
  <Wires />
</FlgNet></NetworkSource>
          <ProgrammingLanguage>LAD</ProgrammingLanguage>
        </AttributeList>
      </SW.Blocks.CompileUnit>
    </ObjectList>
  </SW.Blocks.OB>
</Document>
""",
    "prodiag/Parent_FB.xml": r"""<?xml version="1.0" encoding="utf-8"?>
<Document>
  <SW.Blocks.FB ID="0">
    <AttributeList>
      <Interface><Sections xmlns="http://www.siemens.com/automation/Openness/SW/Interface/v5">
  <Section Name="Static">
    <Member Name="Dev1" Datatype="&quot;Dev_FB&quot;" />
  </Section>
</Sections></Interface>
      <Name>Parent_FB</Name>
    </AttributeList>
    <ObjectList>
      <SW.Blocks.CompileUnit ID="1" CompositionName="CompileUnits">
        <AttributeList>
          <NetworkSource><FlgNet xmlns="http://www.siemens.com/automation/Openness/SW/NetworkSource/FlgNet/v4">
  <Parts>
    <Access Scope="GlobalVariable" UId="21">
      <Symbol><Component Name="Sig_DB" /><Component Name="s1" /></Symbol>
    </Access>
    <Access Scope="LiteralConstant" UId="22">
      <Constant><ConstantType>String</ConstantType><ConstantValue>'Press 1'</ConstantValue></Constant>
    </Access>
    <Call UId="23">
      <CallInfo Name="Dev_FB" BlockType="FB">
        <Instance Scope="LocalVariable" UId="24"><Component Name="Dev1" /></Instance>
        <Parameter Name="IN" Section="Input" Type="Bool" />
        <Parameter Name="Name" Section="Input" Type="String" />
      </CallInfo>
    </Call>
  </Parts>
  <Wires>
    <Wire UId="25"><Powerrail /><NameCon UId="23" Name="en" /></Wire>
    <Wire UId="26"><IdentCon UId="21" /><NameCon UId="23" Name="IN" /></Wire>
    <Wire UId="27"><IdentCon UId="22" /><NameCon UId="23" Name="Name" /></Wire>
  </Wires>
</FlgNet></NetworkSource>
          <ProgrammingLanguage>LAD</ProgrammingLanguage>
        </AttributeList>
      </SW.Blocks.CompileUnit>
    </ObjectList>
  </SW.Blocks.FB>
</Document>
""",
    "tia_logic/PLC1/Blocks/Mini_FC.xml": r"""<?xml version="1.0" encoding="utf-8"?>
<Document>
  <Engineering version="V17" />
  <SW.Blocks.FC ID="0">
    <AttributeList>
      <Interface><Sections xmlns="http://www.siemens.com/automation/Openness/SW/Interface/v5">
  <Section Name="Input" />
  <Section Name="Output" />
  <Section Name="Temp"><Member Name="Tmp" Datatype="Bool" /></Section>
  <Section Name="Return"><Member Name="Ret_Val" Datatype="Void" /></Section>
</Sections></Interface>
      <Name>Mini_FC</Name>
      <Number>1</Number>
      <ProgrammingLanguage>LAD</ProgrammingLanguage>
    </AttributeList>
    <ObjectList>
      <SW.Blocks.CompileUnit ID="5" CompositionName="CompileUnits">
        <AttributeList>
          <NetworkSource><FlgNet xmlns="http://www.siemens.com/automation/Openness/SW/NetworkSource/FlgNet/v4">
  <Parts>
    <Access Scope="GlobalVariable" UId="21"><Symbol><Component Name="DB1" /><Component Name="Sens A" /></Symbol></Access>
    <Access Scope="GlobalVariable" UId="22"><Symbol><Component Name="DB1" /><Component Name="Self" /></Symbol></Access>
    <Access Scope="GlobalVariable" UId="23"><Symbol><Component Name="DB1" /><Component Name="Blocked" /></Symbol></Access>
    <Access Scope="GlobalVariable" UId="24"><Symbol><Component Name="DB1" /><Component Name="Motor" /></Symbol></Access>
    <Part Name="Contact" UId="31" />
    <Part Name="Contact" UId="32" />
    <Part Name="O" UId="33"><TemplateValue Name="Card" Type="Cardinality">2</TemplateValue></Part>
    <Part Name="Contact" UId="34"><Negated Name="operand" /></Part>
    <Part Name="Coil" UId="35" />
  </Parts>
  <Wires>
    <Wire UId="40"><Powerrail /><NameCon UId="31" Name="in" /><NameCon UId="32" Name="in" /></Wire>
    <Wire UId="41"><IdentCon UId="21" /><NameCon UId="31" Name="operand" /></Wire>
    <Wire UId="42"><NameCon UId="31" Name="out" /><NameCon UId="33" Name="in1" /></Wire>
    <Wire UId="43"><IdentCon UId="22" /><NameCon UId="32" Name="operand" /></Wire>
    <Wire UId="44"><NameCon UId="32" Name="out" /><NameCon UId="33" Name="in2" /></Wire>
    <Wire UId="45"><NameCon UId="33" Name="out" /><NameCon UId="34" Name="in" /></Wire>
    <Wire UId="46"><IdentCon UId="23" /><NameCon UId="34" Name="operand" /></Wire>
    <Wire UId="47"><NameCon UId="34" Name="out" /><NameCon UId="35" Name="in" /></Wire>
    <Wire UId="48"><IdentCon UId="24" /><NameCon UId="35" Name="operand" /></Wire>
  </Wires>
</FlgNet></NetworkSource>
          <ProgrammingLanguage>LAD</ProgrammingLanguage>
        </AttributeList>
        <ObjectList>
          <MultilingualText ID="6" CompositionName="Title"><ObjectList>
            <MultilingualTextItem ID="7" CompositionName="Items"><AttributeList><Culture>ru-RU</Culture><Text>Пуск двигателя</Text></AttributeList></MultilingualTextItem>
          </ObjectList></MultilingualText>
        </ObjectList>
      </SW.Blocks.CompileUnit>
      <SW.Blocks.CompileUnit ID="8" CompositionName="CompileUnits">
        <AttributeList>
          <NetworkSource><FlgNet xmlns="http://www.siemens.com/automation/Openness/SW/NetworkSource/FlgNet/v4">
  <Parts>
    <Access Scope="GlobalVariable" UId="21"><Symbol><Component Name="DB1" /><Component Name="Level" /></Symbol></Access>
    <Access Scope="LiteralConstant" UId="22"><Constant><ConstantType>Int</ConstantType><ConstantValue>50</ConstantValue></Constant></Access>
    <Access Scope="GlobalVariable" UId="23"><Symbol><Component Name="DB1" /><Component Name="Ready" /></Symbol></Access>
    <Access Scope="GlobalVariable" UId="24"><Symbol><Component Name="DB1" /><Component Name="Full" /></Symbol></Access>
    <Access Scope="GlobalVariable" UId="25"><Symbol><Component Name="DB1" /><Component Name="Reset" /></Symbol></Access>
    <Access Scope="GlobalVariable" UId="26"><Symbol><Component Name="DB1" /><Component Name="Total" /></Symbol></Access>
    <Part Name="Ge" UId="31"><TemplateValue Name="SrcType" Type="Type">Int</TemplateValue></Part>
    <Part Name="Contact" UId="32" />
    <Part Name="SCoil" UId="33" />
    <Part Name="Contact" UId="34" />
    <Part Name="RCoil" UId="35" />
    <Part Name="Calc" UId="36" />
  </Parts>
  <Wires>
    <Wire UId="40"><Powerrail /><NameCon UId="31" Name="pre" /><NameCon UId="34" Name="in" /><NameCon UId="36" Name="en" /></Wire>
    <Wire UId="41"><IdentCon UId="21" /><NameCon UId="31" Name="in1" /></Wire>
    <Wire UId="42"><IdentCon UId="22" /><NameCon UId="31" Name="in2" /></Wire>
    <Wire UId="43"><NameCon UId="31" Name="out" /><NameCon UId="32" Name="in" /></Wire>
    <Wire UId="44"><IdentCon UId="23" /><NameCon UId="32" Name="operand" /></Wire>
    <Wire UId="45"><NameCon UId="32" Name="out" /><NameCon UId="33" Name="in" /></Wire>
    <Wire UId="46"><IdentCon UId="24" /><NameCon UId="33" Name="operand" /></Wire>
    <Wire UId="47"><IdentCon UId="25" /><NameCon UId="34" Name="operand" /></Wire>
    <Wire UId="48"><NameCon UId="34" Name="out" /><NameCon UId="35" Name="in" /></Wire>
    <Wire UId="49"><IdentCon UId="24" /><NameCon UId="35" Name="operand" /></Wire>
    <Wire UId="50"><NameCon UId="36" Name="out" /><IdentCon UId="26" /></Wire>
  </Wires>
</FlgNet></NetworkSource>
          <ProgrammingLanguage>LAD</ProgrammingLanguage>
        </AttributeList>
      </SW.Blocks.CompileUnit>
      <SW.Blocks.CompileUnit ID="9" CompositionName="CompileUnits">
        <AttributeList>
          <NetworkSource><FlgNet xmlns="http://www.siemens.com/automation/Openness/SW/NetworkSource/FlgNet/v4">
  <Parts>
    <Access Scope="GlobalVariable" UId="21"><Symbol><Component Name="DB1" /><Component Name="Go" /></Symbol></Access>
    <Access Scope="GlobalVariable" UId="22"><Symbol><Component Name="DB1" /><Component Name="Alarm" /></Symbol></Access>
    <Access Scope="GlobalVariable" UId="23"><Symbol><Component Name="DB1" /><Component Name="Running" /></Symbol></Access>
    <Access Scope="GlobalVariable" UId="24"><Symbol><Component Name="DB1" /><Component Name="Delay" /></Symbol></Access>
    <Access Scope="TypedConstant" UId="25"><Constant><ConstantValue>T#300MS</ConstantValue></Constant></Access>
    <Access Scope="GlobalVariable" UId="26"><Symbol><Component Name="DB1" /><Component Name="Done" /></Symbol></Access>
    <Part Name="Contact" UId="31" />
    <Call UId="32">
      <CallInfo Name="Sub_FB" BlockType="FB">
        <Instance Scope="GlobalVariable" UId="33"><Component Name="Sub_FB_IDB" /></Instance>
        <Parameter Name="Start" Section="Input" Type="Bool" />
        <Parameter Name="Block" Section="Input" Type="Bool" />
        <Parameter Name="Run" Section="Output" Type="Bool" />
      </CallInfo>
    </Call>
    <Part Name="TON" Version="1.0" UId="34"><Instance Scope="GlobalVariable" UId="35"><Component Name="DB1" /><Component Name="TonRun" /></Instance><TemplateValue Name="time_type" Type="Type">Time</TemplateValue></Part>
    <Part Name="Contact" UId="36" />
    <Part Name="Coil" UId="37" />
  </Parts>
  <Wires>
    <Wire UId="40"><Powerrail /><NameCon UId="31" Name="in" /><NameCon UId="32" Name="en" /></Wire>
    <Wire UId="41"><IdentCon UId="21" /><NameCon UId="31" Name="operand" /></Wire>
    <Wire UId="42"><NameCon UId="31" Name="out" /><NameCon UId="32" Name="Start" /></Wire>
    <Wire UId="43"><IdentCon UId="22" /><NameCon UId="32" Name="Block" /></Wire>
    <Wire UId="44"><NameCon UId="32" Name="Run" /><IdentCon UId="23" /></Wire>
    <Wire UId="45"><IdentCon UId="23" /><NameCon UId="34" Name="IN" /></Wire>
    <Wire UId="46"><IdentCon UId="25" /><NameCon UId="34" Name="PT" /></Wire>
    <Wire UId="47"><NameCon UId="34" Name="Q" /><NameCon UId="36" Name="in" /></Wire>
    <Wire UId="48"><IdentCon UId="24" /><NameCon UId="36" Name="operand" /></Wire>
    <Wire UId="49"><NameCon UId="36" Name="out" /><NameCon UId="37" Name="in" /></Wire>
    <Wire UId="50"><IdentCon UId="26" /><NameCon UId="37" Name="operand" /></Wire>
  </Wires>
</FlgNet></NetworkSource>
          <ProgrammingLanguage>LAD</ProgrammingLanguage>
        </AttributeList>
      </SW.Blocks.CompileUnit>
    </ObjectList>
  </SW.Blocks.FC>
</Document>
""",
    "tia_logic/PLC1/Blocks/Sub_FB.xml": r"""<?xml version="1.0" encoding="utf-8"?>
<Document>
  <Engineering version="V17" />
  <SW.Blocks.FB ID="0">
    <AttributeList>
      <Interface><Sections xmlns="http://www.siemens.com/automation/Openness/SW/Interface/v5">
  <Section Name="Input"><Member Name="Start" Datatype="Bool" /><Member Name="Block" Datatype="Bool" /></Section>
  <Section Name="Output"><Member Name="Run" Datatype="Bool" /></Section>
  <Section Name="Static"><Member Name="Latch" Datatype="Bool" /></Section>
</Sections></Interface>
      <Name>Sub_FB</Name>
      <Number>2</Number>
      <ProgrammingLanguage>LAD</ProgrammingLanguage>
    </AttributeList>
    <ObjectList>
      <SW.Blocks.CompileUnit ID="5" CompositionName="CompileUnits">
        <AttributeList>
          <NetworkSource><FlgNet xmlns="http://www.siemens.com/automation/Openness/SW/NetworkSource/FlgNet/v4">
  <Parts>
    <Access Scope="LocalVariable" UId="21"><Symbol><Component Name="Start" /></Symbol></Access>
    <Access Scope="LocalVariable" UId="22"><Symbol><Component Name="Block" /></Symbol></Access>
    <Access Scope="LocalVariable" UId="23"><Symbol><Component Name="Run" /></Symbol></Access>
    <Access Scope="GlobalVariable" UId="24"><Symbol><Component Name="DB1" /><Component Name="Estop" /></Symbol></Access>
    <Part Name="Contact" UId="31" />
    <Part Name="Contact" UId="32"><Negated Name="operand" /></Part>
    <Part Name="Contact" UId="33"><Negated Name="operand" /></Part>
    <Part Name="Coil" UId="34" />
  </Parts>
  <Wires>
    <Wire UId="40"><Powerrail /><NameCon UId="31" Name="in" /></Wire>
    <Wire UId="41"><IdentCon UId="21" /><NameCon UId="31" Name="operand" /></Wire>
    <Wire UId="42"><NameCon UId="31" Name="out" /><NameCon UId="32" Name="in" /></Wire>
    <Wire UId="43"><IdentCon UId="22" /><NameCon UId="32" Name="operand" /></Wire>
    <Wire UId="44"><NameCon UId="32" Name="out" /><NameCon UId="33" Name="in" /></Wire>
    <Wire UId="45"><IdentCon UId="24" /><NameCon UId="33" Name="operand" /></Wire>
    <Wire UId="46"><NameCon UId="33" Name="out" /><NameCon UId="34" Name="in" /></Wire>
    <Wire UId="47"><IdentCon UId="23" /><NameCon UId="34" Name="operand" /></Wire>
  </Wires>
</FlgNet></NetworkSource>
          <ProgrammingLanguage>LAD</ProgrammingLanguage>
        </AttributeList>
      </SW.Blocks.CompileUnit>
    </ObjectList>
  </SW.Blocks.FB>
</Document>
""",
}

SOURCE_NEWLINES = {
    "prodiag/Dev_FB.xml": "\r\n",
    "prodiag/Glob_DB.xml": "\r\n",
    "prodiag/Main_OB.xml": "\r\n",
    "prodiag/Parent_FB.xml": "\r\n",
    "tia_logic/PLC1/Blocks/Mini_FC.xml": "\r\n",
    "tia_logic/PLC1/Blocks/Sub_FB.xml": "\r\n",
}


def materialize_tia_fixtures(root: Path, group: str) -> Path:
    """Write one synthetic fixture group beneath a pytest temporary directory."""
    if group not in {"prodiag", "tia_logic"}:
        raise ValueError(f"Unknown synthetic fixture group: {group}")
    for relative, content in SYNTHETIC_XML.items():
        parts = relative.split("/")
        if parts[0] != group:
            continue
        target = root.joinpath(*parts[1:])
        target.parent.mkdir(parents=True, exist_ok=True)
        text = content.replace("\n", SOURCE_NEWLINES[relative])
        target.write_bytes(text.encode("utf-8"))
    return root
